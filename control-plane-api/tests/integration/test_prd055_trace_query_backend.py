"""PRD-055 后端 API、租户权限、持久损坏和迁移状态机聚焦测试。

测试数据库均由完整迁移生成，随后只写最小租户夹具。用例验证成功、异常和权限路径，
并直接攻击数据库门禁，证明服务层条件不是租户隔离与不可变事实的唯一防线。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.request_logs import query as query_service
from metahub_control.request_logs.routes import router as request_logs_router
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试凭据只服务于临时数据库中的后端权限断言，绝不写入业务日志或交付配置。
ADMIN_PASSWORD = "LocalAdmin!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
OTHER_ADMIN_PASSWORD = "OtherAdminLocal!2026"


def _sqlite_url(path: Path) -> str:
    """把沙箱临时数据库路径转换成项目支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只注册认证和 RequestLog 路由的聚焦测试应用。"""

    app = create_service_app("control-plane-api", "PRD-055 Backend Test API")
    app.include_router(auth_router)
    app.include_router(request_logs_router)
    return app


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str]:
    """应用全部迁移，创建 viewer，并写三条可区分模型语义的安全调用日志。"""

    database_url = _sqlite_url(tmp_path / "prd055.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd055-test-auth-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    tenant_id = seed.tenant_id
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status,
          created_at, updated_at
        )
        VALUES(
          'user_prd055_viewer', ?, 'viewer-prd055@example.com', 'PRD055 Viewer',
          ?, 'viewer', 'active', '2026-07-28T00:00:00Z', '2026-07-28T00:00:00Z'
        )
        """,
        (tenant_id, hash_password(VIEWER_PASSWORD)),
    )
    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? ORDER BY provider_id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? ORDER BY model_id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    virtual_key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY virtual_key_id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    assert provider is not None and model is not None and virtual_key is not None
    rows = [
        ("req_prd055_model_a", "trc_prd055_model_a", "same-model", "other-model", "2026-07-28T01:00:00Z"),
        ("req_prd055_model_b", "trc_prd055_model_b", "same-model", "same-model", "2026-07-28T01:01:00Z"),
        ("req_prd055_model_c", "trc_prd055_model_c", "other-model", "same-model", "2026-07-28T01:02:00Z"),
    ]
    # 0034 起 RequestLog 必须引用先行冻结的不可变 claim；本用例不消费 Policy，
    # 因此使用 NULL snapshot + disabled，并保持 tenant/trace/Virtual Key 完全一致。
    connection.executemany(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        )
        VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
        """,
        [
            (
                tenant_id,
                request_id,
                trace_id,
                virtual_key["virtual_key_id"],
                created_at,
            )
            for request_id, trace_id, _request_model, _resolved_model, created_at in rows
        ],
    )
    connection.executemany(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          endpoint, request_model, resolved_model, provider_code, status, error_code,
          latency_ms, input_tokens, output_tokens, total_tokens, estimated_cost_amount,
          currency, price_schema_version, created_at, updated_at
        )
        VALUES(
          ?, ?, ?, ?, ?, ?, '/v1/chat/completions', ?, ?, 'local_openai',
          'success', NULL, 10, 2, 3, 5, 0.001, 'CNY', 'test-v1', ?, ?
        )
        """,
        [
            (
                request_id,
                tenant_id,
                trace_id,
                virtual_key["virtual_key_id"],
                provider["provider_id"],
                model["model_id"],
                request_model,
                resolved_model,
                created_at,
                created_at,
            )
            for request_id, trace_id, request_model, resolved_model, created_at in rows
        ],
    )
    connection.commit()
    connection.close()
    # 仅返回非敏感数据库地址和 tenant 标识，访问令牌始终留在单个测试协程内。
    return database_url, tenant_id


def _add_isolation_users(database_url: str, tenant_id: str) -> None:
    """增加同租户 developer 与第二租户 admin，供 owner/tenant 同形边界测试使用。

    参数：pytest 独占数据库 URL 与默认租户 ID。返回：无。
    租户约束：第二租户先建根记录，再写各自 user；密码只保存不可逆哈希。
    """

    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO tenants(
              tenant_id, slug, name, service_mode, status, created_at, updated_at
            )
            VALUES(
              'tenant_prd055_other', 'prd055-other', 'PRD055 Other Tenant',
              'internal_only', 'active', '2026-07-28T00:00:00Z', '2026-07-28T00:00:00Z'
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO users(
              user_id, tenant_id, email, display_name, password_hash, role, status,
              created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, 'active', '2026-07-28T00:00:00Z', '2026-07-28T00:00:00Z')
            """,
            (
                (
                    "user_prd055_developer",
                    tenant_id,
                    "developer-prd055@example.com",
                    "PRD055 Developer",
                    hash_password(DEVELOPER_PASSWORD),
                    "developer",
                ),
                (
                    "user_prd055_other_admin",
                    "tenant_prd055_other",
                    "admin-prd055-other@example.com",
                    "PRD055 Other Admin",
                    hash_password(OTHER_ADMIN_PASSWORD),
                    "admin",
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()


async def _login(
    client: AsyncClient,
    email: str,
    password: str,
    *,
    tenant_slug: str = "default",
) -> str:
    """登录指定测试租户的固定用户并返回只驻留当前测试进程的访问令牌。

    参数：HTTP 客户端、测试邮箱/密码及显式租户 slug。返回：短生命周期访问令牌。
    安全约束：令牌不写日志、不落盘，也不在不同测试数据库之间复用。
    """

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 令牌只用于当前测试请求头，不打印、不持久化，也不跨测试数据库复用。
    return response.json()["access_token"]


def _wire_filters(payload: dict[str, object]) -> str:
    """生成高级查询公开合同要求的 Base64URL 无填充筛选。"""

    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _saved_filter_payload(name: str = "我的排障筛选") -> dict[str, object]:
    """返回合法且不含身份字段的 SavedFilter 创建正文。"""

    return {
        "name": name,
        "filters": {"v": 1, "provider_code": "local_openai"},
        "sort_by": "created_at",
        "sort_direction": "desc",
    }


def _assert_private_headers(response) -> None:
    """断言成功响应不会进入浏览器、代理或共享缓存。"""

    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    assert "authorization" in {
        item.strip().lower() for item in response.headers["vary"].split(",")
    }


def test_empty_0045_migration_round_trip_preserves_schema_integrity(tmp_path: Path) -> None:
    """空业务事实时逐版回退后再回退/重放 0045，且不会残留半状态。"""

    database_url = _sqlite_url(tmp_path / "prd055-round-trip.db")
    applied = apply_migrations(database_url)
    assert "0045" in applied.applied
    # 根据本次真实迁移结果撤销全部 0045 后版本；静态枚举会在新增迁移后从错误栈顶
    # 开始回滚，既无法验证新 down，也会让 0045 历史合同产生误报。
    later_versions = tuple(
        sorted(
            {
                version
                for version in (*applied.applied, *applied.skipped)
                if version > "0045"
            },
            reverse=True,
        )
    )
    for later_version in later_versions:
        later_rollback = rollback_last_migration(database_url)
        assert later_rollback.rolled_back == later_version
    rolled_back = rollback_last_migration(database_url)
    assert rolled_back.rolled_back == "0045"

    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%prd055'"
    ).fetchone()[0] == 0
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()

    reapplied = apply_migrations(database_url)
    assert "0045" in reapplied.applied
    connection = connect_database(database_url)
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE type='trigger' AND name LIKE '%prd055'
        """
    ).fetchone()[0] == 15
    owner_foreign_keys = [
        row
        for row in connection.execute("PRAGMA foreign_key_list(saved_filters)").fetchall()
        if row["table"] == "users"
    ]
    # owner 必须由同一个复合外键同时绑定 tenant_id 和 user_id，不能退化为仅 user_id。
    assert len({row["id"] for row in owner_foreign_keys}) == 1
    assert {(row["from"], row["to"]) for row in owner_foreign_keys} == {
        ("tenant_id", "tenant_id"),
        ("owner_user_id", "user_id"),
    }
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


@pytest.mark.anyio
async def test_saved_filter_raw_json_is_bounded_and_rbac_precedes_parsing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """创建/更新/删除主流程成功，重复键与 wire 异常拒绝，viewer 先返回权限错误。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        viewer = await _login(client, "viewer-prd055@example.com", VIEWER_PASSWORD)
        admin_headers = {"Authorization": f"Bearer {admin}"}
        raw = json.dumps(_saved_filter_payload(), ensure_ascii=False).encode("utf-8")
        created = await client.post(
            "/request-logs/saved-filters",
            content=raw,
            headers={**admin_headers, "Content-Type": "application/json; charset=utf-8"},
        )
        assert created.status_code == 201, created.text
        saved = created.json()["saved_filter"]
        _assert_private_headers(created)

        listed = await client.get("/request-logs/saved-filters", headers=admin_headers)
        assert listed.status_code == 200
        assert [item["saved_filter_id"] for item in listed.json()["saved_filters"]] == [
            saved["saved_filter_id"]
        ]
        _assert_private_headers(listed)

        patched = await client.patch(
            f"/request-logs/saved-filters/{saved['saved_filter_id']}",
            json={"name": "重命名筛选", "version": saved["version"]},
            headers=admin_headers,
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["saved_filter"]["version"] == 2
        _assert_private_headers(patched)

        duplicate_key = await client.post(
            "/request-logs/saved-filters",
            content=(
                b'{"name":"a","name":"b","filters":{"v":1},'
                b'"sort_by":"created_at","sort_direction":"desc"}'
            ),
            headers={**admin_headers, "Content-Type": "application/json"},
        )
        unknown_field = await client.post(
            "/request-logs/saved-filters",
            json={**_saved_filter_payload("未知字段"), "tenant_id": "other"},
            headers=admin_headers,
        )
        wrong_type = await client.post(
            "/request-logs/saved-filters",
            content=raw,
            headers={**admin_headers, "Content-Type": "text/plain"},
        )
        oversized_declared = await client.post(
            "/request-logs/saved-filters",
            content=raw,
            headers={
                **admin_headers,
                "Content-Type": "application/json",
                "Content-Length": "8193",
            },
        )
        viewer_malformed = await client.post(
            "/request-logs/saved-filters",
            content=b"{",
            headers={
                "Authorization": f"Bearer {viewer}",
                "Content-Type": "text/plain",
            },
        )
        anonymous_malformed = await client.post(
            "/request-logs/saved-filters",
            content=b"{",
            headers={"Content-Type": "text/plain"},
        )
        deleted = await client.delete(
            f"/request-logs/saved-filters/{saved['saved_filter_id']}?version=2",
            headers=admin_headers,
        )

    for response in (duplicate_key, unknown_field, wrong_type, oversized_declared):
        assert response.status_code == 422
        assert response.json()["code"] == "saved_filter.invalid"
        assert response.json()["request_id"]
    assert viewer_malformed.status_code == 403
    assert viewer_malformed.json()["code"] == "auth.permission_denied"
    assert anonymous_malformed.status_code == 401
    assert anonymous_malformed.json()["code"] == "auth.token_missing"
    assert deleted.status_code == 204
    _assert_private_headers(deleted)


@pytest.mark.anyio
async def test_saved_filter_name_version_delete_and_recreate_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证规范化重名、乐观锁、deleted 不可恢复以及删除后同名新 ID。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        created = await client.post(
            "/request-logs/saved-filters",
            json=_saved_filter_payload("Case Filter"),
            headers=headers,
        )
        assert created.status_code == 201
        saved = created.json()["saved_filter"]
        duplicate = await client.post(
            "/request-logs/saved-filters",
            json=_saved_filter_payload("case filter"),
            headers=headers,
        )
        stale_update = await client.patch(
            f"/request-logs/saved-filters/{saved['saved_filter_id']}",
            json={"name": "过期更新", "version": 2},
            headers=headers,
        )
        updated = await client.patch(
            f"/request-logs/saved-filters/{saved['saved_filter_id']}",
            json={"name": "更新筛选", "version": 1},
            headers=headers,
        )
        stale_delete = await client.delete(
            f"/request-logs/saved-filters/{saved['saved_filter_id']}?version=1",
            headers=headers,
        )
        deleted = await client.delete(
            f"/request-logs/saved-filters/{saved['saved_filter_id']}?version=2",
            headers=headers,
        )
        deleted_update = await client.patch(
            f"/request-logs/saved-filters/{saved['saved_filter_id']}",
            json={"name": "禁止复活", "version": 3},
            headers=headers,
        )
        deleted_again = await client.delete(
            f"/request-logs/saved-filters/{saved['saved_filter_id']}?version=3",
            headers=headers,
        )
        deleted_apply = await client.get(
            "/request-logs",
            params={"saved_filter_id": saved["saved_filter_id"]},
            headers=headers,
        )
        recreated = await client.post(
            "/request-logs/saved-filters",
            json=_saved_filter_payload("更新筛选"),
            headers=headers,
        )
        listed = await client.get("/request-logs/saved-filters", headers=headers)

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "saved_filter.name_conflict"
    for response in (stale_update, stale_delete):
        assert response.status_code == 409
        assert response.json()["code"] == "saved_filter.version_conflict"
    assert updated.status_code == 200
    assert updated.json()["saved_filter"]["version"] == 2
    assert deleted.status_code == 204
    for response in (deleted_update, deleted_again, deleted_apply):
        assert response.status_code == 404
        assert response.json()["code"] == "saved_filter.not_found"
    assert recreated.status_code == 201
    replacement = recreated.json()["saved_filter"]
    assert replacement["saved_filter_id"] != saved["saved_filter_id"]
    assert [item["saved_filter_id"] for item in listed.json()["saved_filters"]] == [
        replacement["saved_filter_id"]
    ]


@pytest.mark.anyio
async def test_saved_filter_active_limit_is_exactly_fifty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """每个 tenant+owner 最多 50 条 active；软删除释放配额且允许同名新 ID。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        created_filters = []
        for index in range(50):
            response = await client.post(
                "/request-logs/saved-filters",
                json=_saved_filter_payload(f"边界筛选-{index:02d}"),
                headers=headers,
            )
            assert response.status_code == 201, response.text
            created_filters.append(response.json()["saved_filter"])
        overflow = await client.post(
            "/request-logs/saved-filters",
            json=_saved_filter_payload("第 51 条"),
            headers=headers,
        )
        released = created_filters[-1]
        deleted = await client.delete(
            f"/request-logs/saved-filters/{released['saved_filter_id']}?version=1",
            headers=headers,
        )
        replacement = await client.post(
            "/request-logs/saved-filters",
            json=_saved_filter_payload(released["name"]),
            headers=headers,
        )
        listed = await client.get("/request-logs/saved-filters", headers=headers)

    assert overflow.status_code == 409
    assert overflow.json()["code"] == "saved_filter.limit_exceeded"
    assert deleted.status_code == 204
    assert replacement.status_code == 201
    assert replacement.json()["saved_filter"]["saved_filter_id"] != released["saved_filter_id"]
    assert len(listed.json()["saved_filters"]) == 50


@pytest.mark.anyio
async def test_saved_filters_enforce_tenant_and_owner_even_for_admin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同租户跨 owner 与跨租户访问均返回同形 404，admin 也没有个人筛选越权能力。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    _add_isolation_users(database_url, tenant_id)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await _login(
            client,
            "developer-prd055@example.com",
            DEVELOPER_PASSWORD,
        )
        other_admin_token = await _login(
            client,
            "admin-prd055-other@example.com",
            OTHER_ADMIN_PASSWORD,
            tenant_slug="prd055-other",
        )
        headers_by_owner = {
            "admin": {"Authorization": f"Bearer {admin_token}"},
            "developer": {"Authorization": f"Bearer {developer_token}"},
            "other": {"Authorization": f"Bearer {other_admin_token}"},
        }
        created_by_owner = {}
        for owner, headers in headers_by_owner.items():
            response = await client.post(
                "/request-logs/saved-filters",
                json=_saved_filter_payload("同名个人筛选"),
                headers=headers,
            )
            assert response.status_code == 201, response.text
            created_by_owner[owner] = response.json()["saved_filter"]

        for owner, headers in headers_by_owner.items():
            listed = await client.get("/request-logs/saved-filters", headers=headers)
            assert listed.status_code == 200
            assert {
                item["saved_filter_id"] for item in listed.json()["saved_filters"]
            } == {created_by_owner[owner]["saved_filter_id"]}

        admin_headers = headers_by_owner["admin"]
        developer_headers = headers_by_owner["developer"]
        other_headers = headers_by_owner["other"]
        admin_filter_id = created_by_owner["admin"]["saved_filter_id"]
        developer_filter_id = created_by_owner["developer"]["saved_filter_id"]
        other_filter_id = created_by_owner["other"]["saved_filter_id"]
        forbidden = (
            await client.get(
                "/request-logs",
                params={"saved_filter_id": developer_filter_id},
                headers=admin_headers,
            ),
            await client.patch(
                f"/request-logs/saved-filters/{developer_filter_id}",
                json={"name": "管理员越权重命名", "version": 1},
                headers=admin_headers,
            ),
            await client.delete(
                f"/request-logs/saved-filters/{developer_filter_id}?version=1",
                headers=admin_headers,
            ),
            await client.get(
                "/request-logs",
                params={"saved_filter_id": admin_filter_id},
                headers=developer_headers,
            ),
            await client.get(
                "/request-logs",
                params={"saved_filter_id": other_filter_id},
                headers=admin_headers,
            ),
            await client.get(
                "/request-logs",
                params={"saved_filter_id": admin_filter_id},
                headers=other_headers,
            ),
        )
        default_query = await client.get("/request-logs", headers=admin_headers)
        developer_query = await client.get("/request-logs", headers=developer_headers)
        other_query = await client.get("/request-logs", headers=other_headers)

    for response in forbidden:
        assert response.status_code == 404
        assert response.json()["code"] == "saved_filter.not_found"
        assert response.json()["request_id"]
    assert default_query.status_code == 200 and default_query.json()["total"] == 3
    assert developer_query.status_code == 200 and developer_query.json()["total"] == 3
    assert other_query.status_code == 200 and other_query.json()["total"] == 0


@pytest.mark.anyio
async def test_full_filter_sort_pagination_and_empty_event_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实 API 覆盖精确交集、八种排序、分页边界、重复稳定性和空结果审计。"""

    database_url, _ = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        exact_filters = {
            "v": 1,
            "trace_id": "trc_prd055_model_b",
            "status": "success",
            "request_model": "same-model",
            "resolved_model": "same-model",
            "provider_code": "local_openai",
            "created_from": "2026-07-28T00:00:00Z",
            "created_to": "2026-07-28T02:00:00Z",
            "latency_ms_min": 10,
            "latency_ms_max": 10,
            "total_tokens_min": 5,
            "total_tokens_max": 5,
            "cost_amount_min": "0.001",
            "cost_amount_max": "0.001",
            "currency": "CNY",
            "terminal_stage": "completed",
        }
        exact = await client.get(
            "/request-logs",
            params={"filters": _wire_filters(exact_filters)},
            headers=headers,
        )
        assert exact.status_code == 200, exact.text
        assert [item["request_id"] for item in exact.json()["request_logs"]] == [
            "req_prd055_model_b"
        ]

        sorted_ids: dict[tuple[str, str], list[str]] = {}
        for sort_by in ("created_at", "latency_ms", "total_tokens", "estimated_cost_amount"):
            for direction in ("asc", "desc"):
                response = await client.get(
                    "/request-logs",
                    params={
                        "filters": _wire_filters({"v": 1}),
                        "sort_by": sort_by,
                        "sort_direction": direction,
                        "limit": 3,
                    },
                    headers=headers,
                )
                assert response.status_code == 200, response.text
                sorted_ids[(sort_by, direction)] = [
                    item["request_id"] for item in response.json()["request_logs"]
                ]
        expected_ascending = [
            "req_prd055_model_a",
            "req_prd055_model_b",
            "req_prd055_model_c",
        ]
        expected_descending = list(reversed(expected_ascending))
        for sort_by in ("created_at", "latency_ms", "total_tokens", "estimated_cost_amount"):
            assert sorted_ids[(sort_by, "asc")] == expected_ascending
            assert sorted_ids[(sort_by, "desc")] == expected_descending

        pages = []
        for offset in range(4):
            response = await client.get(
                "/request-logs",
                params={
                    "filters": _wire_filters({"v": 1}),
                    "limit": 1,
                    "offset": offset,
                },
                headers=headers,
            )
            assert response.status_code == 200
            pages.append(response.json())
        repeated_middle = await client.get(
            "/request-logs",
            params={"filters": _wire_filters({"v": 1}), "limit": 1, "offset": 1},
            headers=headers,
        )
        empty = await client.get(
            "/request-logs",
            params={
                "filters": _wire_filters(
                    {"v": 1, "trace_id": "trc_prd055_not_found"}
                )
            },
            headers=headers,
        )

    assert [page["total"] for page in pages] == [3, 3, 3, 3]
    assert [page["has_more"] for page in pages] == [True, True, False, False]
    assert [len(page["request_logs"]) for page in pages] == [1, 1, 1, 0]
    assert repeated_middle.json()["request_logs"] == pages[1]["request_logs"]
    assert empty.status_code == 200
    assert empty.json()["request_logs"] == []
    assert empty.json()["total"] == 0
    assert empty.json()["has_more"] is False

    connection = connect_database(database_url)
    empty_event = connection.execute(
        """
        SELECT matched_count, returned_count, has_more
        FROM request_log_query_events WHERE query_event_id=?
        """,
        (empty.json()["query_event_id"],),
    ).fetchone()
    event_count = connection.execute(
        "SELECT COUNT(*) FROM request_log_query_events"
    ).fetchone()[0]
    connection.close()
    assert empty_event is not None
    assert tuple(empty_event) == (0, 0, 0)
    assert event_count == 15


@pytest.mark.anyio
async def test_query_modes_pagination_event_contract_and_snapshot_floor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """legacy/inline 模型结果不同，分页事实与事件一致，快照零值统一拒绝。"""

    database_url, _ = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        legacy = await client.get(
            "/request-logs?model=same-model&limit=1&offset=1",
            headers=headers,
        )
        inline = await client.get(
            "/request-logs",
            params={
                "filters": _wire_filters(
                    {
                        "v": 1,
                        "request_model": "same-model",
                        "resolved_model": "same-model",
                    }
                )
            },
            headers=headers,
        )
        bad_inline = await client.get(
            "/request-logs",
            params={"filters": _wire_filters({"v": 1, "snapshot_version": 0})},
            headers=headers,
        )
        bad_legacy = await client.get("/request-logs?snapshot_version=0", headers=headers)

    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["total"] == 3
    assert len(legacy.json()["request_logs"]) == 1
    assert legacy.json()["has_more"] is True
    _assert_private_headers(legacy)
    assert inline.status_code == 200, inline.text
    assert inline.json()["total"] == 1
    assert [item["request_id"] for item in inline.json()["request_logs"]] == [
        "req_prd055_model_b"
    ]
    for response in (bad_inline, bad_legacy):
        assert response.status_code == 422
        assert response.json()["code"] == "request_log.query_invalid"

    connection = connect_database(database_url)
    legacy_event = connection.execute(
        """
        SELECT filter_keys_json, filter_fingerprint, query_offset, returned_count,
               matched_count, has_more
        FROM request_log_query_events
        WHERE operation_request_id=?
        """,
        (legacy.json()["request_id"],),
    ).fetchone()
    inline_event = connection.execute(
        """
        SELECT filter_keys_json, filter_fingerprint
        FROM request_log_query_events
        WHERE operation_request_id=?
        """,
        (inline.json()["request_id"],),
    ).fetchone()
    connection.close()
    assert legacy_event is not None and inline_event is not None
    assert json.loads(legacy_event["filter_keys_json"]) == ["model"]
    assert json.loads(inline_event["filter_keys_json"]) == [
        "request_model",
        "resolved_model",
    ]
    assert legacy_event["filter_fingerprint"] != inline_event["filter_fingerprint"]
    assert bool(legacy_event["has_more"]) == (
        legacy_event["query_offset"] + legacy_event["returned_count"]
        < legacy_event["matched_count"]
    )


@pytest.mark.anyio
async def test_dto_mapping_failure_does_not_write_success_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """持久日志无法映射为安全 DTO 时返回 503，且不能留下成功 query event。"""

    database_url, _ = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        before = connect_database(database_url)
        before_count = before.execute(
            "SELECT COUNT(*) AS count FROM request_log_query_events"
        ).fetchone()["count"]
        before.close()

        def _broken_mapper(_row):
            """模拟持久行与安全 DTO 合同不一致。"""

            # 映射异常用于证明审计事件尚未写入，不包含任何真实日志正文或密钥。
            raise ValueError("broken persisted row")

        monkeypatch.setattr(query_service, "_request_log_info", _broken_mapper)
        response = await client.get(
            "/request-logs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "request_log.query_unavailable"
    connection = connect_database(database_url)
    after_count = connection.execute(
        "SELECT COUNT(*) AS count FROM request_log_query_events"
    ).fetchone()["count"]
    connection.close()
    assert after_count == before_count


@pytest.mark.anyio
async def test_query_audit_failure_closes_without_returning_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """QueryEvent 提交被数据库拒绝时返回固定 503，不能泄露已读取的日志列表。"""

    database_url, _ = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute(
        """
        CREATE TRIGGER test_prd055_query_event_failure
        BEFORE INSERT ON request_log_query_events FOR EACH ROW
        BEGIN SELECT RAISE(ABORT,'fixed_test_rejection'); END
        """
    )
    connection.commit()
    connection.close()

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.get(
            "/request-logs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 503
    payload = response.json()
    assert payload["code"] == "request_log.query_audit_unavailable"
    assert payload["request_id"] and payload["trace_id"]
    assert response.headers["x-request-id"] == payload["request_id"]
    # 已读取的 DTO、模型值和底层触发器正文都不能穿过统一错误边界。
    assert "request_logs" not in payload
    assert "same-model" not in response.text
    assert "fixed_test_rejection" not in response.text
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM request_log_query_events"
    ).fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_corrupt_persisted_saved_filter_maps_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """模拟磁盘损坏后，列表与可信 ID 应用都返回 503，而不是误报用户 422。"""

    database_url, _ = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        created = await client.post(
            "/request-logs/saved-filters",
            json=_saved_filter_payload(),
            headers=headers,
        )
        assert created.status_code == 201
        saved_filter_id = created.json()["saved_filter"]["saved_filter_id"]

        connection = connect_database(database_url)
        # 测试仅移除状态和 payload 更新门禁以模拟磁盘或人工运维损坏；
        # 生产写路径不能绕过这两层触发器，因此这里不会削弱真实运行时保护。
        connection.execute("DROP TRIGGER trg_saved_filters_state_update_prd055")
        connection.execute("DROP TRIGGER trg_saved_filters_payload_update_prd055")
        connection.execute(
            "UPDATE saved_filters SET filter_json=? WHERE saved_filter_id=?",
            ('{"v":0}', saved_filter_id),
        )
        connection.commit()
        connection.close()

        listed = await client.get("/request-logs/saved-filters", headers=headers)
        applied = await client.get(
            "/request-logs",
            params={"saved_filter_id": saved_filter_id},
            headers=headers,
        )

    for response in (listed, applied):
        assert response.status_code == 503
        assert response.json()["code"] == "saved_filter.unavailable"


@pytest.mark.anyio
async def test_migration_blocks_illegal_initial_states_replace_and_time_regression(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """数据库拒绝高版本/删除初态、REPLACE、时间回退及伪造 QueryEvent 状态。"""

    database_url, _ = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        created = await client.post(
            "/request-logs/saved-filters",
            json=_saved_filter_payload(),
            headers=headers,
        )
        queried = await client.get("/request-logs?limit=1", headers=headers)
    assert created.status_code == 201 and queried.status_code == 200
    saved_filter_id = created.json()["saved_filter"]["saved_filter_id"]
    query_event_id = queried.json()["query_event_id"]

    connection = connect_database(database_url)
    with pytest.raises(sqlite3.IntegrityError, match="saved_filter_state_invalid"):
        connection.execute(
            """
            INSERT INTO saved_filters(
              saved_filter_id, tenant_id, owner_user_id, name, name_normalized,
              filter_version, filter_json, sort_by, sort_direction, version,
              status, created_at, updated_at, deleted_at
            )
            SELECT
              'sf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', tenant_id, owner_user_id,
              '非法初态', '非法初态', filter_version, filter_json, sort_by,
              sort_direction, 2, 'active', created_at, created_at, NULL
            FROM saved_filters WHERE saved_filter_id=?
            """,
            (saved_filter_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="saved_filter_state_invalid"):
        connection.execute(
            """
            UPDATE saved_filters
            SET version=version+1, updated_at=updated_at
            WHERE saved_filter_id=?
            """,
            (saved_filter_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="saved_filter_immutable"):
        connection.execute(
            """
            INSERT OR REPLACE INTO saved_filters
            SELECT * FROM saved_filters WHERE saved_filter_id=?
            """,
            (saved_filter_id,),
        )
    invalid_saved_filters = (
        ("sf_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "未知字段", '{"tenant_id":"other","v":1}'),
        ("sf_cccccccccccccccccccccccccccccccc", "重复字段", '{"v":1,"status":"failed","status":"success"}'),
        ("sf_dddddddddddddddddddddddddddddddd", "嵌套字段", '{"v":1,"status":{"nested":true}}'),
    )
    for invalid_id, invalid_name, invalid_filter_json in invalid_saved_filters:
        with pytest.raises(sqlite3.IntegrityError, match="saved_filter_payload_invalid"):
            connection.execute(
                """
                INSERT INTO saved_filters(
                  saved_filter_id, tenant_id, owner_user_id, name, name_normalized,
                  filter_version, filter_json, sort_by, sort_direction, version,
                  status, created_at, updated_at, deleted_at
                )
                SELECT ?, tenant_id, owner_user_id, ?, ?, filter_version, ?,
                       sort_by, sort_direction, version, status, created_at, updated_at, deleted_at
                FROM saved_filters WHERE saved_filter_id=?
                """,
                (
                    invalid_id,
                    invalid_name,
                    invalid_name,
                    invalid_filter_json,
                    saved_filter_id,
                ),
            )
    with pytest.raises(sqlite3.IntegrityError, match="saved_filter_state_invalid"):
        connection.execute(
            """
            UPDATE saved_filters
            SET status='deleted',
                filter_json='{"status":"failed","v":1}',
                version=version+1,
                updated_at=strftime('%Y-%m-%dT%H:%M:%SZ',unixepoch(updated_at)+1),
                deleted_at=strftime('%Y-%m-%dT%H:%M:%SZ',unixepoch(updated_at)+1)
            WHERE saved_filter_id=?
            """,
            (saved_filter_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="request_log_query_event_state_invalid"):
        connection.execute(
            """
            INSERT INTO request_log_query_events(
              query_event_id, event_id, tenant_id, actor_user_id, actor_session_id,
              filter_version, filter_fingerprint, filter_keys_json, saved_filter_id,
              sort_by, sort_direction, query_limit, query_offset, matched_count,
              returned_count, has_more, query_duration_ms, operation_request_id,
              operation_trace_id, event_dispatch_status, event_dispatch_attempts,
              event_next_attempt_at, event_dispatched_at, event_claim_token,
              event_lease_owner_id, event_lease_expires_at, event_last_error_code,
              executed_at, created_at, updated_at
            )
            SELECT
              'lqe_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
              'lqevt_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
              tenant_id, actor_user_id, actor_session_id, filter_version,
              filter_fingerprint, filter_keys_json, saved_filter_id, sort_by,
              sort_direction, query_limit, query_offset, matched_count,
              returned_count, has_more, query_duration_ms, operation_request_id,
              operation_trace_id, 'processing', 1, NULL, NULL,
              '1234567890abcdef', 'worker_prd055',
              datetime(updated_at, '+1 minute'), NULL, executed_at, created_at, updated_at
            FROM request_log_query_events WHERE query_event_id=?
            """,
            (query_event_id,),
        )
    invalid_key_sets = (
        ("e" * 32, "f" * 32, '["status","status"]'),
        ("1" * 32, "2" * 32, '["provider_key"]'),
        ("3" * 32, "4" * 32, '["status","request_id"]'),
    )
    for query_suffix, event_suffix, invalid_keys_json in invalid_key_sets:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="request_log_query_event_keys_invalid",
        ):
            connection.execute(
                """
                INSERT INTO request_log_query_events(
                  query_event_id, event_id, tenant_id, actor_user_id, actor_session_id,
                  filter_version, filter_fingerprint, filter_keys_json, saved_filter_id,
                  sort_by, sort_direction, query_limit, query_offset, matched_count,
                  returned_count, has_more, query_duration_ms, operation_request_id,
                  operation_trace_id, event_dispatch_status, event_dispatch_attempts,
                  event_next_attempt_at, event_dispatched_at, event_claim_token,
                  event_lease_owner_id, event_lease_expires_at, event_last_error_code,
                  executed_at, created_at, updated_at
                )
                SELECT ?, ?, tenant_id, actor_user_id, actor_session_id,
                       filter_version, filter_fingerprint, ?, saved_filter_id,
                       sort_by, sort_direction, query_limit, query_offset, matched_count,
                       returned_count, has_more, query_duration_ms, operation_request_id,
                       operation_trace_id, event_dispatch_status, event_dispatch_attempts,
                       event_next_attempt_at, event_dispatched_at, event_claim_token,
                       event_lease_owner_id, event_lease_expires_at, event_last_error_code,
                       executed_at, created_at, updated_at
                FROM request_log_query_events WHERE query_event_id=?
                """,
                (
                    "lqe_" + query_suffix,
                    "lqevt_" + event_suffix,
                    invalid_keys_json,
                    query_event_id,
                ),
            )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO request_log_query_events
            SELECT
              'lqe_cccccccccccccccccccccccccccccccc',
              'lqevt_dddddddddddddddddddddddddddddddd',
              tenant_id, actor_user_id, actor_session_id, filter_version,
              filter_fingerprint, filter_keys_json, saved_filter_id, sort_by,
              sort_direction, query_limit, query_offset, matched_count,
              returned_count, 1-has_more, query_duration_ms, operation_request_id,
              operation_trace_id, event_dispatch_status, event_dispatch_attempts,
              event_next_attempt_at, event_dispatched_at, event_claim_token,
              event_lease_owner_id, event_lease_expires_at, event_last_error_code,
              executed_at, created_at, updated_at
            FROM request_log_query_events WHERE query_event_id=?
            """,
            (query_event_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="request_log_query_event_immutable"):
        connection.execute(
            """
            INSERT OR REPLACE INTO request_log_query_events
            SELECT * FROM request_log_query_events WHERE query_event_id=?
            """,
            (query_event_id,),
        )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.rollback()
    connection.close()
