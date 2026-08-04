"""PRD-043 用量 CSV 导出集成测试。

覆盖同步/异步闭环、request_id 幂等、worker lease、租户/RBAC、CSV 公式防护、
严格审计与过期元数据。测试数据只含安全账本字段，任何模拟秘密都只用于“不泄露”断言。
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sqlite3
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.usage import export_service
from metahub_control.usage.export_service import (
    claim_next_usage_export,
    process_claimed_usage_export,
)
from metahub_control.usage.routes import router as usage_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.webhooks import (
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
    canonical_json_bytes,
    verify_webhook_signature,
    webhook_signature_header,
)

# 测试密码只用于临时 SQLite 身份夹具，覆盖 admin、developer、viewer 与跨租户权限边界。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
OTHER_PASSWORD = "OtherAdminLocal!2026"


def _sqlite_url(path: Path) -> str:
    """把测试路径转换为项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只包含认证和 Usage 路由的控制面测试应用。"""

    app = create_service_app("control-plane-api", "Usage Export Test API")
    app.include_router(auth_router)
    app.include_router(usage_router)
    return app


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """准备两个租户、三种角色和两条带价格快照的 Usage 记录。"""

    database_url = _sqlite_url(tmp_path / "metahub-usage-export.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-usage-export-auth-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    tenant_id = seed.tenant_id
    admin = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND email='admin@metahub.local'",
        (tenant_id,),
    ).fetchone()
    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_code='local_openai'",
        (tenant_id,),
    ).fetchone()
    model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
        (tenant_id,),
    ).fetchone()
    virtual_key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (tenant_id,),
    ).fetchone()
    assert admin and provider and model and virtual_key
    now = "2026-07-17T01:00:00Z"
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        [
            (
                "user_prd043_developer",
                tenant_id,
                "developer@example.com",
                "Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
                now,
                now,
            ),
            (
                "user_prd043_viewer",
                tenant_id,
                "viewer@example.com",
                "Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
                now,
                now,
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO projects(
          project_id, tenant_id, project_code, name, status, created_by, updated_by, created_at, updated_at
        ) VALUES('prj_prd043', ?, 'prd043', ?, 'active', ?, ?, ?, ?)
        """,
        (tenant_id, "\u200b=SUM(1,1)", admin["user_id"], admin["user_id"], now, now),
    )
    connection.execute(
        "UPDATE virtual_keys SET name=?, project_id=? WHERE tenant_id=? AND virtual_key_id=?",
        ("\x1b@CMD()", "prj_prd043", tenant_id, virtual_key["virtual_key_id"]),
    )
    for index, (tokens, cost, created_at) in enumerate(
        [(150, "0.3500", "2026-07-16T09:00:00Z"), (50, "0.0100", "2026-07-16T10:00:00Z")],
        start=1,
    ):
        request_id = f"req_prd043_usage_{index}"
        snapshot_id = f"prs_prd043_{index}"
        # PRD-043 账本夹具创建于未发布基线；0034 后也要先 claim，保证导出读取的是
        # 与生产相同的 tenant+request_id 关联，而不是依赖旧单列外键。
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id, request_id, trace_id, virtual_key_id,
              snapshot_version, policy_snapshot_status, claimed_at
            ) VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
            """,
            (
                tenant_id,
                request_id,
                f"trc_prd043_usage_{index}",
                virtual_key["virtual_key_id"],
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
              endpoint, request_model, resolved_model, provider_code, status, error_code,
              latency_ms, project_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, '/v1/chat/completions', 'company-default',
                     'default-chat', 'local_openai', 'success', NULL, 20, 'prj_prd043', ?, ?)
            """,
            (
                request_id,
                tenant_id,
                f"trc_prd043_usage_{index}",
                virtual_key["virtual_key_id"],
                provider["provider_id"],
                model["model_id"],
                created_at,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO price_snapshots(
              price_snapshot_id, tenant_id, request_id, model_id, provider_id, provider_code,
              currency, input_price_per_1k, output_price_per_1k, price_schema_version,
              billing_dimensions, provider_raw_mapping, source, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, 'local_openai', 'CNY', 0.0010, 0.0050,
                     'price-v1', '{}', '{}', 'model_catalog', 'active', ?, ?)
            """,
            (
                snapshot_id,
                tenant_id,
                request_id,
                model["model_id"],
                provider["provider_id"],
                created_at,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO usage_ledger(
              usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens,
              total_tokens, cache_tokens, raw_usage, normalized_usage,
              cost_amount, cost_cny, cost_usd, currency, price_schema_version,
              estimated, usage_source, price_snapshot_id, model_id, provider_id,
              virtual_key_id, project_id, endpoint, request_model, resolved_model,
              provider_code, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, 0, ?, '{}', ?, ?, 0, 'CNY', 'price-v1',
                     0, 'upstream', ?, ?, ?, ?, 'prj_prd043', '/v1/chat/completions',
                     'company-default', 'default-chat', 'local_openai', ?, ?)
            """,
            (
                f"usg_prd043_{index}",
                tenant_id,
                request_id,
                tokens - 20,
                20,
                tokens,
                '{"api_key":"sk-prd043-must-not-export"}',
                cost,
                cost,
                snapshot_id,
                model["model_id"],
                provider["provider_id"],
                virtual_key["virtual_key_id"],
                created_at,
                created_at,
            ),
        )
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('tenant_prd043_other', 'other-prd043', 'Other', 'internal_only', 'active', ?, ?)
        """,
        (now, now),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        ) VALUES('user_prd043_other_admin', 'tenant_prd043_other', 'admin@other.local',
                 'Other Admin', ?, 'admin', 'active', ?, ?)
        """,
        (hash_password(OTHER_PASSWORD), now, now),
    )
    connection.commit()
    connection.close()
    return {
        "database_url": database_url,
        "tenant_id": tenant_id,
        "model_id": model["model_id"],
        "virtual_key_id": virtual_key["virtual_key_id"],
    }


async def _login(client: AsyncClient, tenant_slug: str, email: str, password: str) -> str:
    """登录指定租户测试用户并返回短生命周期令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 后续请求必须使用真实认证返回令牌，不能绕过后端角色和租户校验。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_usage_export_sync_csv_security_audit_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同步主流程：固定 CSV、公式防护、审计/事件原子写入和 request_id 幂等。"""

    state = _prepare_database(monkeypatch, tmp_path)
    monkeypatch.setenv("METAHUB_USAGE_EXPORT_SYNC_ROW_LIMIT", "5")
    app = _build_app()

    def fail_webhook_dispatch(*_args, **_kwargs):
        """模拟客户 Webhook 故障，任务和完成事件仍必须提交。"""

        # 接收端故障必须发生在提交后派发阶段，用于证明任务和审计不会被回滚。
        raise RuntimeError("receiver unavailable")

    monkeypatch.setattr(
        "metahub_control.webhooks.service.dispatch_webhook_event",
        fail_webhook_dispatch,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "default", "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {token}", "X-Request-ID": "req_prd043_sync_export"}
        export_url = (
            "/usage/export.csv?start_date=2026-07-16&end_date=2026-07-16"
            f"&project_id=prj_prd043&model_id={state['model_id']}"
            f"&virtual_key_id={state['virtual_key_id']}"
        )
        exported, replayed = await asyncio.gather(
            client.get(export_url, headers=headers),
            client.get(export_url, headers=headers),
        )
        mismatched = await client.get(
            f"/usage/export.csv?virtual_key_id={state['virtual_key_id']}",
            headers=headers,
        )
        empty = await client.get(
            "/usage/export.csv?model_id=model-does-not-exist",
            headers={"Authorization": f"Bearer {token}", "X-Request-ID": "req_prd043_empty"},
        )

    assert exported.status_code == 200, exported.text
    assert exported.content.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in exported.content
    assert exported.headers["content-type"] == "text/csv; charset=utf-8"
    assert exported.headers["cache-control"] == "no-store, private"
    assert exported.headers["x-content-type-options"] == "nosniff"
    rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"), newline="")))
    assert len(rows) == 3
    assert rows[0][0:4] == ["usage_ledger_id", "tenant_id", "request_id", "trace_id"]
    project_index = rows[0].index("project_name")
    key_name_index = rows[0].index("virtual_key_name")
    assert all(row[project_index].startswith("'") for row in rows[1:])
    assert all(row[key_name_index].startswith("'") for row in rows[1:])
    assert rows[1][rows[0].index("cost_cny")] == "0.35"
    assert "sk-prd043-must-not-export" not in exported.text
    assert replayed.content == exported.content
    empty_rows = list(csv.reader(io.StringIO(empty.content.decode("utf-8-sig"), newline="")))
    assert empty.status_code == 200
    assert len(empty_rows) == 1
    assert len(empty_rows[0]) == 42
    assert mismatched.status_code == 422
    assert mismatched.json()["code"] == "usage.export_filter_invalid"

    connection = connect_database(state["database_url"])
    job = connection.execute(
        "SELECT * FROM usage_export_jobs WHERE tenant_id=? AND request_id='req_prd043_sync_export'",
        (state["tenant_id"],),
    ).fetchone()
    assert job is not None
    assert job["status"] == "completed"
    assert job["download_count"] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE tenant_id=? AND object_id=?",
        (state["tenant_id"], job["usage_export_job_id"]),
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM usage_export_events WHERE tenant_id=? AND usage_export_job_id=?",
        (state["tenant_id"], job["usage_export_job_id"]),
    ).fetchone()[0] == 1
    event_row = connection.execute(
        "SELECT * FROM usage_export_events WHERE tenant_id=? AND usage_export_job_id=?",
        (state["tenant_id"], job["usage_export_job_id"]),
    ).fetchone()
    payload = json.loads(event_row["payload_json"])
    assert set(payload) == {
        "usage_export_job_id",
        "row_count",
        "generation_mode",
        "content_size_bytes",
        "status",
    }
    assert "usage.exported" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    event = build_webhook_event(
        tenant_id=state["tenant_id"],
        event_type="usage.exported",
        event_id="evt_prd043_hmac",
        actor_id=event_row["actor_user_id"],
        object_id=job["usage_export_job_id"],
        trace_id=event_row["trace_id"],
        payload=payload,
    )
    body = canonical_json_bytes(event.to_payload())
    timestamp = int(time.time())
    signature = webhook_signature_header("whsec_prd043_test", timestamp, body)
    assert verify_webhook_signature(
        signature,
        "whsec_prd043_test",
        body,
        now_seconds=timestamp,
    )
    assert job["csv_content"] is not None
    corrupted = bytearray(job["csv_content"])
    corrupted[-1] = (corrupted[-1] + 1) % 255
    connection.execute(
        "UPDATE usage_export_jobs SET csv_content=? WHERE tenant_id=? AND usage_export_job_id=?",
        (bytes(corrupted), state["tenant_id"], job["usage_export_job_id"]),
    )
    connection.commit()
    connection.close()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        corrupted_download = await client.get(
            f"/usage/exports/{job['usage_export_job_id']}/download",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert corrupted_download.status_code == 503
    assert corrupted_download.json()["code"] == "usage.export_unavailable"


@pytest.mark.anyio
async def test_usage_export_async_lease_download_expiry_and_tenant_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异步主流程：202、单 lease、worker 完成、授权下载、过期和跨租户 404。"""

    state = _prepare_database(monkeypatch, tmp_path)
    monkeypatch.setenv("METAHUB_USAGE_EXPORT_SYNC_ROW_LIMIT", "1")
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "default", "admin@metahub.local", ADMIN_PASSWORD)
        other_token = await _login(client, "other-prd043", "admin@other.local", OTHER_PASSWORD)
        accepted = await client.get(
            "/usage/export.csv",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd043_async_export"},
        )
        other_export = await client.get(
            "/usage/export.csv",
            headers={"Authorization": f"Bearer {other_token}", "X-Request-ID": "req_prd043_other_export"},
        )
        assert accepted.status_code == 202, accepted.text
        assert other_export.status_code == 200, other_export.text
        job_id = accepted.json()["export_job"]["usage_export_job_id"]
        pending_download = await client.get(
            f"/usage/exports/{job_id}/download",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert pending_download.status_code == 409
        assert pending_download.json()["code"] == "usage.export_not_ready"

        connection = connect_database(state["database_url"])
        snapshot_row = connection.execute(
            "SELECT snapshot_ledger_sequence FROM usage_export_jobs WHERE tenant_id=? AND usage_export_job_id=?",
            (state["tenant_id"], job_id),
        ).fetchone()
        assert snapshot_row is not None
        # 模拟任务计数完成后 Gateway 又写入同一秒的 Usage；时间 cutoff 无法区分，必须由只增序号水位排除。
        source = connection.execute(
            "SELECT * FROM usage_ledger WHERE tenant_id=? ORDER BY rowid DESC LIMIT 1",
            (state["tenant_id"],),
        ).fetchone()
        assert source is not None
        # 水位测试模拟的同秒晚到请求同样先 claim，不能因测试注入绕过日志不可变边界。
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id, request_id, trace_id, virtual_key_id,
              snapshot_version, policy_snapshot_status, claimed_at
            ) VALUES(?, 'req_prd043_same_second_late', 'trc_prd043_same_second_late', ?,
                     NULL, 'disabled', ?)
            """,
            (state["tenant_id"], source["virtual_key_id"], source["created_at"]),
        )
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
              endpoint, request_model, resolved_model, provider_code, status, error_code,
              latency_ms, project_id, created_at, updated_at
            ) VALUES('req_prd043_same_second_late', ?, 'trc_prd043_same_second_late', ?, ?, ?,
                     '/v1/chat/completions', 'company-default', 'default-chat', 'local_openai',
                     'success', NULL, 20, 'prj_prd043', ?, ?)
            """,
            (
                state["tenant_id"],
                source["virtual_key_id"],
                source["provider_id"],
                source["model_id"],
                source["created_at"],
                source["created_at"],
            ),
        )
        connection.execute(
            """
            INSERT INTO usage_ledger(
              usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens,
              total_tokens, cache_tokens, raw_usage, normalized_usage,
              cost_amount, cost_cny, cost_usd, currency, price_schema_version,
              estimated, usage_source, price_snapshot_id, model_id, provider_id,
              virtual_key_id, project_id, endpoint, request_model, resolved_model,
              provider_code, created_at, updated_at
            ) VALUES('usg_prd043_same_second_late', ?, 'req_prd043_same_second_late',
                     80, 20, 100, 0, '{}', '{}', 0.02, 0.02, 0, 'CNY', 'price-v1',
                     0, 'upstream', NULL, ?, ?, ?, 'prj_prd043', '/v1/chat/completions',
                     'company-default', 'default-chat', 'local_openai', ?, ?)
            """,
            (
                state["tenant_id"],
                source["model_id"],
                source["provider_id"],
                source["virtual_key_id"],
                source["created_at"],
                source["created_at"],
            ),
        )
        late_sequence = connection.execute(
            """
            SELECT usage_export_sequence FROM usage_export_ledger_sequences
            WHERE tenant_id=? AND usage_ledger_id='usg_prd043_same_second_late'
            """,
            (state["tenant_id"],),
        ).fetchone()[0]
        assert late_sequence > snapshot_row["snapshot_ledger_sequence"]
        # 已分配导出序号的账本禁止删除，否则 SQLite 可能复用身份并破坏历史快照边界。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM usage_ledger WHERE tenant_id=? AND usage_ledger_id='usg_prd043_same_second_late'",
                (state["tenant_id"],),
            )
        connection.commit()
        connection.close()

        claim = claim_next_usage_export(app.state.config)
        assert claim is not None and claim.usage_export_job_id == job_id
        assert claim_next_usage_export(app.state.config) is None
        connection = connect_database(state["database_url"])
        connection.execute(
            "UPDATE usage_export_jobs SET lease_until='2000-01-01T00:00:00Z' WHERE usage_export_job_id=?",
            (job_id,),
        )
        connection.commit()
        connection.close()
        takeover = claim_next_usage_export(app.state.config)
        assert takeover is not None and takeover.lease_token != claim.lease_token
        assert process_claimed_usage_export(app.state.config, claim) is False
        assert process_claimed_usage_export(app.state.config, takeover) is True
        status = await client.get(
            f"/usage/exports/{job_id}", headers={"Authorization": f"Bearer {admin_token}"}
        )
        downloaded = await client.get(
            f"/usage/exports/{job_id}/download",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd043_download"},
        )

        connection = connect_database(state["database_url"])
        other_job_id = connection.execute(
            "SELECT usage_export_job_id FROM usage_export_jobs WHERE tenant_id='tenant_prd043_other'"
        ).fetchone()[0]
        connection.close()
        cross_tenant = await client.get(
            f"/usage/exports/{other_job_id}", headers={"Authorization": f"Bearer {admin_token}"}
        )

    assert status.status_code == 200
    assert status.json()["export_job"]["status"] == "completed"
    assert status.json()["export_job"]["row_count"] == 2
    original_size = status.json()["export_job"]["content_size_bytes"]
    original_hash = status.json()["export_job"]["content_sha256"]
    assert downloaded.status_code == 200
    assert downloaded.content.startswith(b"\xef\xbb\xbf")
    downloaded_rows = list(csv.reader(io.StringIO(downloaded.content.decode("utf-8-sig"), newline="")))
    assert len(downloaded_rows) == 3
    assert "usg_prd043_same_second_late" not in {row[0] for row in downloaded_rows[1:]}
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["code"] == "usage.export_not_found"

    connection = connect_database(state["database_url"])
    connection.execute(
        "UPDATE usage_export_jobs SET expires_at='2000-01-01T00:00:00Z' WHERE usage_export_job_id=?",
        (job_id,),
    )
    connection.commit()
    connection.close()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        expired_status = await client.get(
            f"/usage/exports/{job_id}", headers={"Authorization": f"Bearer {admin_token}"}
        )
        expired_download = await client.get(
            f"/usage/exports/{job_id}/download", headers={"Authorization": f"Bearer {admin_token}"}
        )
    assert expired_status.json()["export_job"]["status"] == "expired"
    assert expired_status.json()["export_job"]["content_size_bytes"] == original_size
    assert expired_status.json()["export_job"]["content_sha256"] == original_hash
    assert expired_download.status_code == 410


@pytest.mark.anyio
async def test_usage_export_admin_only_unknown_query_and_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：developer/viewer/匿名拒绝，未知 query 422，硬上限 413。"""

    _prepare_database(monkeypatch, tmp_path)
    monkeypatch.setenv("METAHUB_USAGE_EXPORT_SYNC_ROW_LIMIT", "1")
    monkeypatch.setenv("METAHUB_USAGE_EXPORT_MAX_ROWS", "1")
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "default", "admin@metahub.local", ADMIN_PASSWORD)
        developer = await _login(client, "default", "developer@example.com", DEVELOPER_PASSWORD)
        viewer = await _login(client, "default", "viewer@example.com", VIEWER_PASSWORD)
        anonymous = await client.get("/usage/export.csv")
        developer_denied = await client.get(
            "/usage/export.csv", headers={"Authorization": f"Bearer {developer}"}
        )
        viewer_denied = await client.get(
            "/usage/export.csv", headers={"Authorization": f"Bearer {viewer}"}
        )
        unknown_query = await client.get(
            "/usage/export.csv?tenant_id=other&limit=1",
            headers={"Authorization": f"Bearer {admin}"},
        )
        too_large = await client.get(
            "/usage/export.csv",
            headers={"Authorization": f"Bearer {admin}", "X-Request-ID": "req_prd043_too_large"},
        )
    assert anonymous.status_code == 401
    assert developer_denied.status_code == 403
    assert viewer_denied.status_code == 403
    assert unknown_query.status_code == 422
    assert unknown_query.json()["code"] == "usage.export_filter_invalid"
    assert too_large.status_code == 413
    assert too_large.json()["code"] == "usage.export_too_large"


@pytest.mark.anyio
async def test_usage_export_generation_exception_and_worker_final_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """生成异常统一 503；异步任务三次失败后终结且下载返回 503。"""

    _prepare_database(monkeypatch, tmp_path)
    monkeypatch.setenv("METAHUB_USAGE_EXPORT_SYNC_ROW_LIMIT", "5")
    app = _build_app()
    original_generate = export_service._generate_csv

    def fail_conversion(*_args, **_kwargs):
        """模拟 CSV 转换中的未知异常，验证 API 只返回统一错误且不泄露原始内容。"""

        # 原始异常文本刻意包含敏感诊断占位，响应断言必须证明它没有穿透 API。
        raise ValueError("unsafe raw conversion detail")

    monkeypatch.setattr(export_service, "_generate_csv", fail_conversion)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "default", "admin@metahub.local", ADMIN_PASSWORD)
        conversion_failed = await client.get(
            "/usage/export.csv",
            headers={"Authorization": f"Bearer {token}", "X-Request-ID": "req_prd043_conversion"},
        )
    assert conversion_failed.status_code == 503
    assert conversion_failed.json()["code"] == "usage.export_unavailable"
    assert "unsafe raw conversion detail" not in conversion_failed.text

    monkeypatch.setattr(export_service, "_generate_csv", original_generate)
    app.state.config.usage_export_sync_row_limit = 1
    app.state.config.usage_export_max_bytes = 1024
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        accepted = await client.get(
            "/usage/export.csv",
            headers={"Authorization": f"Bearer {token}", "X-Request-ID": "req_prd043_worker_fail"},
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["export_job"]["usage_export_job_id"]
        for _attempt in range(3):
            claim = claim_next_usage_export(app.state.config)
            assert claim is not None
            assert process_claimed_usage_export(app.state.config, claim) is False
        failed_status = await client.get(
            f"/usage/exports/{job_id}", headers={"Authorization": f"Bearer {token}"}
        )
        failed_download = await client.get(
            f"/usage/exports/{job_id}/download", headers={"Authorization": f"Bearer {token}"}
        )
    assert failed_status.json()["export_job"]["status"] == "failed"
    assert failed_status.json()["export_job"]["attempt_count"] == 3
    assert failed_download.status_code == 503
    assert failed_download.json()["code"] == "usage.export_unavailable"
