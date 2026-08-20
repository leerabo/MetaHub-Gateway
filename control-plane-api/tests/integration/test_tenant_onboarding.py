"""Focused integration coverage for tenant administrator onboarding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.tenants.routes import router as tenant_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

ADMIN_PASSWORD = "LocalAdmin!2026"


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    database_url = _sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    apply_migrations(database_url)
    apply_seed_data(database_url)
    return database_url


def _build_app():
    app = create_service_app("control-plane-api", "Tenant Onboarding Test API")
    app.include_router(auth_router)
    app.include_router(tenant_router)
    return app


async def _login(client: AsyncClient) -> str:
    response = await client.post(
        "/auth/login",
        json={
            "tenant_slug": "default",
            "email": "admin@metahub.local",
            "password": ADMIN_PASSWORD,
        },
    )
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_onboarding_summary_is_admin_only_tenant_scoped_and_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    tenant_id = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()["tenant_id"]
    connection.execute(
        "UPDATE provider_credentials SET encrypted_key=? WHERE tenant_id=?",
        ("encrypted-sensitive-sentinel", tenant_id),
    )
    connection.execute(
        "UPDATE virtual_keys SET key_hash=? WHERE tenant_id=?",
        ("virtual-key-sensitive-sentinel", tenant_id),
    )
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES('tenant_other','other','Other','internal_only','active',
               '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
          policy_snapshot_status,claimed_at
        ) VALUES('tenant_other','req_other_success','trace_other',NULL,NULL,'disabled',?)
        """,
        ("2026-01-01T00:00:00Z",),
    )
    connection.execute(
        """
        INSERT INTO request_logs(request_id,tenant_id,trace_id,status,created_at,updated_at)
        VALUES('req_other_success','tenant_other','trace_other','success',
               '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')
        """
    )
    connection.commit()
    connection.close()

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _login(client)
        response = await client.get(
            "/tenant/onboarding/summary",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_onboarding_summary",
                "X-Trace-ID": "trace_onboarding_summary",
            },
        )

    assert response.status_code == 200
    body = response.json()
    rendered = json.dumps(body, ensure_ascii=False)
    assert body["tenant_id"] == tenant_id
    assert body["request_id"] == "req_onboarding_summary"
    assert body["trace_id"] == "trace_onboarding_summary"
    assert body["total_steps"] == 8
    assert body["is_complete"] is False
    assert [step["order"] for step in body["steps"]] == list(range(1, 12))
    assert {step["status"] for step in body["steps"]} <= {
        "complete",
        "current",
        "blocked",
        "optional",
    }
    steps = {step["id"]: step for step in body["steps"]}
    assert steps["first-governed-call"]["status"] == "blocked"
    assert "encrypted-sensitive-sentinel" not in rendered
    assert "virtual-key-sensitive-sentinel" not in rendered
    assert "req_other_success" not in rendered
    assert "SELECT " not in rendered
    assert "C:\\" not in rendered


@pytest.mark.anyio
async def test_onboarding_summary_requires_backend_admin_permission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute("UPDATE users SET role='viewer' WHERE email='admin@metahub.local'")
    connection.commit()
    connection.close()

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.get(
            "/tenant/onboarding/summary",
            headers={"X-Request-ID": "req_onboarding_missing"},
        )
        token = await _login(client)
        denied = await client.get(
            "/tenant/onboarding/summary",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_onboarding_denied",
            },
        )

    assert missing.status_code == 401
    assert missing.json()["code"] == "auth.token_missing"
    assert missing.json()["request_id"] == "req_onboarding_missing"
    assert denied.status_code == 403
    assert denied.json()["code"] == "auth.permission_denied"
    assert denied.json()["request_id"] == "req_onboarding_denied"


@pytest.mark.anyio
async def test_onboarding_summary_completes_from_real_tenant_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    tenant = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()
    tenant_id = tenant["tenant_id"]
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND role='admin'",
        (tenant_id,),
    ).fetchone()["user_id"]
    environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant_id,),
    ).fetchone()["environment_id"]
    model_id = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND status='active'",
        (tenant_id,),
    ).fetchone()["model_id"]
    provider_id = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND status='active'",
        (tenant_id,),
    ).fetchone()["provider_id"]
    virtual_key_id = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? AND status='active'",
        (tenant_id,),
    ).fetchone()["virtual_key_id"]
    now = "2026-01-01T00:00:00Z"
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,default_model_id,status,
          created_by,created_at,updated_at,environment_id
        ) VALUES('alias_onboarding',?,'default-chat','dev',?,'active',?,?,?,?)
        """,
        (tenant_id, model_id, user_id, now, now, environment_id),
    )
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,status,created_by,updated_by,created_at,updated_at
        ) VALUES('project_onboarding',?,'onboarding','Onboarding','active',?,?,?,?)
        """,
        (tenant_id, user_id, user_id, now, now),
    )
    connection.execute(
        """
        INSERT INTO applications(
          application_id,tenant_id,project_id,application_code,name,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES('app_onboarding',?,'project_onboarding','console','Console','active',?,?,?,?)
        """,
        (tenant_id, user_id, user_id, now, now),
    )
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
          policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,?,NULL,'disabled',?)
        """,
        (tenant_id, "req_onboarding_success", "trace_onboarding_success", virtual_key_id, now),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,status,
          created_at,updated_at,environment_id,environment_code
        ) VALUES('req_onboarding_success',?,'trace_onboarding_success',?,?,?,'success',?,?,?,?)
        """,
        (tenant_id, virtual_key_id, provider_id, model_id, now, now, environment_id, "dev"),
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,
          raw_usage,normalized_usage,cost_amount,currency,created_at,status,
          total_tokens,cost_cny,cost_usd,estimated,usage_source,updated_at,
          model_id,provider_id,virtual_key_id,environment_id,environment_code
        ) VALUES(
          'usage_onboarding',?,'req_onboarding_success',1,1,'{}','{}',0,'CNY',
          ?,'recorded',2,0,0,0,'upstream',?,?,?,?,?,?
        )
        """,
        (
            tenant_id,
            now,
            now,
            model_id,
            provider_id,
            virtual_key_id,
            environment_id,
            "dev",
        ),
    )
    connection.commit()
    connection.close()

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _login(client)
        response = await client.get(
            "/tenant/onboarding/summary",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["completed_steps"] == body["total_steps"] == 8
    assert body["is_complete"] is True
    mandatory = body["steps"][:8]
    assert all(step["status"] == "complete" for step in mandatory)
