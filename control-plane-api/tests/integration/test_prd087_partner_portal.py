"""PRD-087 伙伴门户 Control API、分润游标与事件恢复集成测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.partner_portal import router as partner_router
from metahub_control.partner_portal.commission import (
    claim_next_commission_cursor,
    fail_claimed_commission,
    materialize_claimed_commission,
)
from metahub_control.partner_portal.outbox import (
    claim_next_partner_event,
    dispatch_claimed_partner_event,
)
from metahub_control.partner_portal.service import normalize_idempotency_key
from metahub_control.users.routes import router as users_router
from metahub_control.webhooks import service as webhook_service
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

PROJECT_ID = "prj_" + "1" * 32
OTHER_CUSTOMER_ID = "pcu_" + "9" * 32
STAMP = "2030-01-01T00:00:00Z"
PRD139_SUPERSEDED_REASON = (
    "PRD-139 cut over partner writes to owner-scoped Channel APIs; "
    "the legacy partners table and Partner Portal runtime are read-only"
)


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _prepare_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    database_url = _database_url(tmp_path / "partner-portal.sqlite3")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "LocalAdmin!2026")
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd087-test-auth-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        admin_id = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? AND role='admin'", (seed.tenant_id,)
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO projects(project_id,tenant_id,project_code,name,owner_user_id,status,
              created_by,updated_by,created_at,updated_at)
            VALUES(?,?,'channel-project','Channel Project',?,'active',?,?,?,?)
            """,
            (PROJECT_ID, seed.tenant_id, admin_id, admin_id, admin_id, STAMP, STAMP),
        )
        connection.commit()
    finally:
        connection.close()
    app = create_service_app("control-plane-api", "PRD-087 Control Test API")
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(partner_router)
    return app, database_url, seed.tenant_id


async def _login(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str, request_id: str, **extra: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": request_id,
        "X-Trace-ID": f"trace_{request_id}",
        **extra,
    }


def _insert_usage_charge(
    database_url: str,
    tenant_id: str,
    *,
    suffix: str,
    amount: str,
    currency: str,
    estimated: bool,
    created_at: str,
):
    connection = connect_database(database_url)
    try:
        metadata = connection.execute(
            """
            SELECT model.model_id,provider.provider_id,provider.provider_code,
                   key.virtual_key_id,environment.environment_id,
                   environment.code environment_code
            FROM models model
            JOIN providers provider
              ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
            JOIN virtual_keys key ON key.tenant_id=model.tenant_id
            JOIN environments environment
              ON environment.tenant_id=model.tenant_id AND environment.code='dev'
            WHERE model.tenant_id=?
            ORDER BY model.model_id,key.virtual_key_id LIMIT 1
            """,
            (tenant_id,),
        ).fetchone()
        assert metadata is not None
        request_id = f"req_prd087_charge_{suffix}"
        trace_id = f"trc_prd087_charge_{suffix}"
        usage_id = f"usg_prd087_charge_{suffix}"
        snapshot_id = f"prs_prd087_charge_{suffix}"
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
              policy_snapshot_status,claimed_at)
            VALUES(?,?,?,?,NULL,'disabled',?)
            """,
            (tenant_id, request_id, trace_id, metadata["virtual_key_id"], created_at),
        )
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,
              status,created_at,updated_at,environment_id,environment_code,project_id)
            VALUES(?,?,?,?,?,?,'success',?,?,?,?,?)
            """,
            (
                request_id,
                tenant_id,
                trace_id,
                metadata["virtual_key_id"],
                metadata["provider_id"],
                metadata["model_id"],
                created_at,
                created_at,
                metadata["environment_id"],
                metadata["environment_code"],
                PROJECT_ID,
            ),
        )
        connection.execute(
            """
            INSERT INTO price_snapshots(
              price_snapshot_id,tenant_id,request_id,model_id,provider_id,provider_code,
              currency,input_price_per_1k,output_price_per_1k,price_schema_version,
              billing_dimensions,provider_raw_mapping,source,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'0.001000','0.002000','price-v1','{}','{}',
                   'model_catalog','active',?,?)
            """,
            (
                snapshot_id,
                tenant_id,
                request_id,
                metadata["model_id"],
                metadata["provider_id"],
                metadata["provider_code"],
                currency,
                created_at,
                created_at,
            ),
        )
        normalized = {
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "cache_tokens": 1,
            "cost_amount": amount,
            "cost_cny": amount if currency == "CNY" else None,
            "cost_usd": amount if currency == "USD" else "0",
            "currency": currency,
            "price_schema_version": "price-v1",
            "price_snapshot_id": snapshot_id,
            "posting_source": "direct",
        }
        connection.execute(
            """
            INSERT INTO usage_ledger(
              usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,total_tokens,
              cache_tokens,raw_usage,normalized_usage,cost_amount,cost_cny,cost_usd,currency,
              price_schema_version,estimated,usage_source,price_snapshot_id,model_id,
              provider_id,virtual_key_id,environment_id,environment_code,provider_code,
              project_id,created_at,updated_at)
            VALUES(?,?,?,3,2,5,1,'{}',?,?,?,?,?,'price-v1',?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                usage_id,
                tenant_id,
                request_id,
                json.dumps(normalized, separators=(",", ":")),
                amount,
                amount if currency == "CNY" else "0",
                amount if currency == "USD" else "0",
                currency,
                int(estimated),
                "estimated" if estimated else "upstream",
                snapshot_id,
                metadata["model_id"],
                metadata["provider_id"],
                metadata["virtual_key_id"],
                metadata["environment_id"],
                metadata["environment_code"],
                metadata["provider_code"],
                PROJECT_ID,
                created_at,
                created_at,
            ),
        )
        entry = connection.execute(
            """
            SELECT finops_ledger_entry_id,ledger_sequence,amount_micros,currency,estimated
            FROM finops_ledger_entries
            WHERE tenant_id=? AND usage_ledger_id=? AND entry_type='usage_charge'
            """,
            (tenant_id, usage_id),
        ).fetchone()
        assert entry is not None
        connection.commit()
        return entry
    finally:
        connection.close()


def _insert_other_tenant_customer(database_url: str) -> None:
    connection = connect_database(database_url)
    try:
        tenant_id = "tenant_prd087_other"
        user_id = "user_prd087_other"
        partner_id = "par_" + "8" * 32
        project_id = "prj_" + "8" * 32
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,'prd087-other','Other Tenant','internal_only','active',?,?)
            """,
            (tenant_id, STAMP, STAMP),
        )
        connection.execute(
            """
            INSERT INTO users(user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at)
            VALUES(?,?,'other@example.invalid','Other User','test-only-hash','viewer','active',?,?)
            """,
            (user_id, tenant_id, STAMP, STAMP),
        )
        connection.execute(
            """
            INSERT INTO projects(project_id,tenant_id,project_code,name,owner_user_id,status,
              created_by,updated_by,created_at,updated_at)
            VALUES(?,?,'other-project','Other Project',?,'active',?,?,?,?)
            """,
            (project_id, tenant_id, user_id, user_id, user_id, STAMP, STAMP),
        )
        connection.execute(
            """
            INSERT INTO partners(partner_id,tenant_id,partner_code,display_name,portal_name,
              brand_primary_color,support_text,default_commission_rate_bps,status,revision,
              idempotency_key_sha256,payload_sha256,created_by_user_id,
              operation_request_id,operation_trace_id,created_at,updated_at)
            VALUES(?,?,'other.channel','Other Partner','Other Portal','#112233',NULL,1000,
              'active',1,?,?,?,'req_other_partner','trace_other_partner',?,?)
            """,
            (partner_id, tenant_id, "a" * 64, "b" * 64, user_id, STAMP, STAMP),
        )
        connection.execute(
            """
            INSERT INTO partner_customers(partner_customer_id,tenant_id,partner_id,project_id,
              customer_code,display_name,status,linked_from_ledger_sequence,
              commission_cutoff_sequence,commission_rate_bps,revision,idempotency_key_sha256,
              payload_sha256,created_by_user_id,disabled_by_user_id,operation_request_id,
              operation_trace_id,disable_request_id,disable_trace_id,created_at,updated_at,disabled_at)
            VALUES(?,?,?,?,'other.customer','Other Customer','active',0,NULL,1000,1,?,?,?,NULL,
              'req_other_customer','trace_other_customer',NULL,NULL,?,?,NULL)
            """,
            (
                OTHER_CUSTOMER_ID,
                tenant_id,
                partner_id,
                project_id,
                "c" * 64,
                "d" * 64,
                user_id,
                STAMP,
                STAMP,
            ),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize(
    "value",
    (
        "short-key-12345",
        "x" * 161,
        "contains space 123456",
        "contains/slash/123456",
    ),
)
def test_idempotency_key_contract_rejects_out_of_bounds_or_unsafe_values(value: str) -> None:
    with pytest.raises(ApiError) as failure:
        normalize_idempotency_key(value)
    assert failure.value.code == "partner.request_invalid"
    assert normalize_idempotency_key("x" * 16) == "x" * 16
    assert normalize_idempotency_key("x" * 160) == "x" * 160


@pytest.mark.anyio
@pytest.mark.skip(reason=PRD139_SUPERSEDED_REASON)
async def test_partner_portal_http_rbac_isolation_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, database_url, tenant_id = _prepare_app(monkeypatch, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        unauthenticated = await client.get("/partners/profile")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["code"] == "auth.token_missing"

        admin_token = await _login(client, "admin@metahub.local", "LocalAdmin!2026")
        created = await client.post(
            "/partners",
            headers=_headers(
                admin_token,
                "req_prd087_partner_create",
                **{"Idempotency-Key": "partner-create-0001"},
            ),
            json={
                "partner_code": "acme.channel",
                "display_name": "Acme Channel",
                "portal_name": "Acme Operations",
                "brand_primary_color": "#176B5B",
                "support_text": "Enterprise support desk",
            },
        )
        assert created.status_code == 201, created.text
        partner = created.json()["partner"]
        assert created.json()["request_id"] == "req_prd087_partner_create"
        assert created.headers["cache-control"] == "no-store, private"

        for index, invalid_branding in enumerate(
            (
                {"portal_name": None},
                {"brand_primary_color": None},
                {"support_text": ""},
            )
        ):
            rejected = await client.patch(
                "/partners/branding",
                headers=_headers(
                    admin_token,
                    f"req_prd087_branding_invalid_{index}",
                    **{"If-Match": "1"},
                ),
                json=invalid_branding,
            )
            assert rejected.status_code == 422
            assert rejected.json()["code"] == "partner.request_invalid"
        unchanged = await client.get(
            "/partners/profile", headers=_headers(admin_token, "req_prd087_branding_unchanged")
        )
        assert unchanged.status_code == 200
        assert unchanged.json()["partner"] == partner

        cleared_support = await client.patch(
            "/partners/branding",
            headers=_headers(
                admin_token,
                "req_prd087_branding_clear_support",
                **{"If-Match": "1"},
            ),
            json={"support_text": None},
        )
        assert cleared_support.status_code == 200, cleared_support.text
        assert cleared_support.json()["partner"]["support_text"] is None
        assert cleared_support.json()["partner"]["revision"] == 2

        replay = await client.post(
            "/partners",
            headers=_headers(
                admin_token,
                "req_prd087_partner_replay",
                **{"Idempotency-Key": "partner-create-0001"},
            ),
            json={
                "partner_code": "acme.channel",
                "display_name": "Acme Channel",
                "portal_name": "Acme Operations",
                "brand_primary_color": "#176B5B",
                "support_text": "Enterprise support desk",
            },
        )
        assert replay.status_code == 201
        assert replay.json()["partner"]["partner_id"] == partner["partner_id"]

        user = await client.post(
            "/users",
            headers=_headers(admin_token, "req_prd087_user_create"),
            json={
                "email": "partner-member@example.invalid",
                "display_name": "Partner Member",
                "password": "LocalMember!2026",
                "role": "developer",
            },
        )
        assert user.status_code == 201, user.text
        member_id = user.json()["user"]["user_id"]
        member_token = await _login(
            client, "partner-member@example.invalid", "LocalMember!2026"
        )
        denied = await client.get(
            "/partners/profile", headers=_headers(member_token, "req_prd087_denied")
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "auth.permission_denied"
        promoted = await client.patch(
            f"/users/{member_id}/role",
            headers=_headers(admin_token, "req_prd087_promote"),
            json={"role": "viewer"},
        )
        assert promoted.status_code == 200
        allowed = await client.get(
            "/partners/profile", headers=_headers(member_token, "req_prd087_allowed")
        )
        assert allowed.status_code == 200
        demoted = await client.patch(
            f"/users/{member_id}/role",
            headers=_headers(admin_token, "req_prd087_demote"),
            json={"role": "developer"},
        )
        assert demoted.status_code == 200
        denied_again = await client.get(
            "/partners/profile", headers=_headers(member_token, "req_prd087_denied_again")
        )
        assert denied_again.status_code == 403

        projects = await client.get(
            "/partners/projects", headers=_headers(admin_token, "req_prd087_projects")
        )
        assert projects.status_code == 200
        assert [item["project_id"] for item in projects.json()["items"]] == [PROJECT_ID]
        customer = await client.post(
            "/partners/customers",
            headers=_headers(
                admin_token,
                "req_prd087_customer_create",
                **{"Idempotency-Key": "customer-create-0001"},
            ),
            json={
                "project_id": PROJECT_ID,
                "customer_code": "north.division",
                "display_name": "North Division",
            },
        )
        assert customer.status_code == 201, customer.text
        customer_id = customer.json()["customer"]["partner_customer_id"]
        assert customer.json()["customer"]["linked_from_ledger_sequence"] >= 0

        for index, invalid_customer in enumerate(
            ({"display_name": None}, {"status": None})
        ):
            rejected = await client.patch(
                f"/partners/customers/{customer_id}",
                headers=_headers(
                    admin_token,
                    f"req_prd087_customer_invalid_{index}",
                    **{"If-Match": "1"},
                ),
                json=invalid_customer,
            )
            assert rejected.status_code == 422
            assert rejected.json()["code"] == "partner.request_invalid"

        customers = await client.get(
            "/partners/customers",
            headers=_headers(admin_token, "req_prd087_customers"),
        )
        assert customers.status_code == 200
        assert customers.json()["items"][0]["usage"]["usage_scope"] == "project_history"
        assert customers.json()["items"][0]["usage"]["failed_count"] == 0

        commission = await client.get(
            "/partners/commission",
            headers=_headers(admin_token, "req_prd087_commission"),
        )
        assert commission.status_code == 200
        assert commission.json()["items"] == []
        csv_response = await client.get(
            "/partners/commission?format=csv",
            headers=_headers(admin_token, "req_prd087_csv"),
        )
        assert csv_response.status_code == 200
        assert csv_response.content.startswith(b"\xef\xbb\xbfperiod_day,")
        assert csv_response.headers["x-content-type-options"] == "nosniff"
        assert csv_response.headers["content-disposition"].endswith('.csv"')
        delivery = await client.get(
            "/partners/delivery-package",
            headers=_headers(admin_token, "req_prd087_delivery"),
        )
        assert delivery.status_code == 200
        package = delivery.json()["package"]
        assert package["partner_code"] == "acme.channel"
        assert package["customers"][0]["customer_code"] == "north.division"
        assert not ({"token", "endpoint", "prompt", "response"} & set(delivery.json()))

        _insert_other_tenant_customer(database_url)
        cross = await client.get(
            f"/partners/commission?customer_id={OTHER_CUSTOMER_ID}",
            headers=_headers(admin_token, "req_prd087_cross_tenant"),
        )
        assert cross.status_code == 404
        assert cross.json()["code"] == "partner.customer_not_found"

    connection = connect_database(database_url)
    try:
        outbox = connection.execute(
            "SELECT payload_json FROM partner_customer_outbox WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        payload = json.loads(outbox["payload_json"])
        assert set(payload) == {
            "schema_version",
            "partner_id",
            "partner_customer_id",
            "customer_code",
            "project_id",
            "status",
            "commission_rate_bps",
        }
        assert payload["partner_customer_id"] == customer_id
    finally:
        connection.close()

    current = datetime(2030, 1, 1, tzinfo=UTC)
    claim = claim_next_commission_cursor(app.state.config, worker_id="commission-test", now=current)
    assert claim is not None
    connection = connect_database(database_url)
    try:
        leased = connection.execute(
            "SELECT status,scan_upper_sequence FROM partner_commission_cursors WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        assert tuple(leased) == ("leased", claim.scan_upper_sequence)
    finally:
        connection.close()
    assert materialize_claimed_commission(app.state.config, claim, now=current) == 0
    connection = connect_database(database_url)
    try:
        pending = connection.execute(
            """
            SELECT status,scan_upper_sequence,next_run_at FROM partner_commission_cursors
            WHERE tenant_id=?
            """,
            (tenant_id,),
        ).fetchone()
        assert pending["status"] == "pending"
        assert pending["scan_upper_sequence"] is None
        assert pending["next_run_at"] == "2030-01-01T00:00:02Z"
    finally:
        connection.close()
    assert (
        claim_next_commission_cursor(
            app.state.config, worker_id="commission-test", now=current + timedelta(seconds=1)
        )
        is None
    )
    retry_claim = claim_next_commission_cursor(
        app.state.config, worker_id="commission-test", now=current + timedelta(seconds=3)
    )
    assert retry_claim is not None
    assert fail_claimed_commission(
        app.state.config, retry_claim, now=current + timedelta(seconds=3)
    )

    first_charge = _insert_usage_charge(
        database_url,
        tenant_id,
        suffix="one",
        amount="1.234567",
        currency="CNY",
        estimated=False,
        created_at="2030-01-01T00:00:09Z",
    )
    first_claim = claim_next_commission_cursor(
        app.state.config, worker_id="commission-one", now=current + timedelta(seconds=9)
    )
    assert first_claim is not None
    assert first_claim.scan_upper_sequence == first_charge["ledger_sequence"]
    assert materialize_claimed_commission(
        app.state.config, first_claim, now=current + timedelta(seconds=9)
    ) == 1
    assert (
        materialize_claimed_commission(
            app.state.config, first_claim, now=current + timedelta(seconds=9)
        )
        is None
    )

    second_charge = _insert_usage_charge(
        database_url,
        tenant_id,
        suffix="two",
        amount="2.000005",
        currency="USD",
        estimated=True,
        created_at="2030-01-01T00:00:11Z",
    )
    assert (
        claim_next_commission_cursor(
            app.state.config, worker_id="commission-too-early", now=current + timedelta(seconds=10)
        )
        is None
    )
    old_claim = claim_next_commission_cursor(
        app.state.config, worker_id="commission-old", now=current + timedelta(seconds=12)
    )
    assert old_claim is not None
    assert old_claim.scan_upper_sequence == second_charge["ledger_sequence"]
    assert (
        claim_next_commission_cursor(
            app.state.config, worker_id="commission-concurrent", now=current + timedelta(seconds=12)
        )
        is None
    )
    takeover_claim = claim_next_commission_cursor(
        app.state.config, worker_id="commission-takeover", now=current + timedelta(seconds=103)
    )
    assert takeover_claim is not None
    assert takeover_claim.scan_upper_sequence == old_claim.scan_upper_sequence
    assert (
        materialize_claimed_commission(
            app.state.config, old_claim, now=current + timedelta(seconds=103)
        )
        is None
    )
    assert materialize_claimed_commission(
        app.state.config, takeover_claim, now=current + timedelta(seconds=103)
    ) == 1

    third_charge = _insert_usage_charge(
        database_url,
        tenant_id,
        suffix="three",
        amount="0.000015",
        currency="CNY",
        estimated=False,
        created_at="2030-01-01T00:01:45Z",
    )
    cutoff_claim = claim_next_commission_cursor(
        app.state.config, worker_id="commission-cutoff", now=current + timedelta(seconds=106)
    )
    assert cutoff_claim is not None
    assert cutoff_claim.scan_upper_sequence == third_charge["ledger_sequence"]
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        disabled = await client.patch(
            f"/partners/customers/{customer_id}",
            headers=_headers(admin_token, "req_prd087_disable", **{"If-Match": "1"}),
            json={"status": "disabled"},
        )
        assert disabled.status_code == 200, disabled.text
        assert (
            disabled.json()["customer"]["commission_cutoff_sequence"]
            == third_charge["ledger_sequence"]
        )
    fourth_charge = _insert_usage_charge(
        database_url,
        tenant_id,
        suffix="after_cutoff",
        amount="9.999999",
        currency="CNY",
        estimated=False,
        created_at="2030-01-01T00:01:46Z",
    )
    assert fourth_charge["ledger_sequence"] > third_charge["ledger_sequence"]
    assert materialize_claimed_commission(
        app.state.config, cutoff_claim, now=current + timedelta(seconds=106)
    ) == 1

    connection = connect_database(database_url)
    try:
        records = connection.execute(
            """
            SELECT source_ledger_sequence,currency,amount_micros,commission_amount_micros,
                   estimated FROM commission_records WHERE tenant_id=?
            ORDER BY source_ledger_sequence
            """,
            (tenant_id,),
        ).fetchall()
        assert [tuple(record) for record in records] == [
            (first_charge["ledger_sequence"], "CNY", 1_234_567, 123_457, 0),
            (second_charge["ledger_sequence"], "USD", 2_000_005, 200_001, 1),
            (third_charge["ledger_sequence"], "CNY", 15, 2, 0),
        ]
        completed = connection.execute(
            """
            SELECT status,last_scanned_ledger_sequence,scan_upper_sequence,next_run_at
            FROM partner_commission_cursors WHERE tenant_id=?
            """,
            (tenant_id,),
        ).fetchone()
        assert tuple(completed) == (
            "complete",
            third_charge["ledger_sequence"],
            None,
            None,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM commission_records WHERE source_ledger_sequence=?",
            (fourth_charge["ledger_sequence"],),
        ).fetchone()[0] == 0
    finally:
        connection.close()

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        report = await client.get(
            "/partners/commission?start_date=2030-01-01&end_date=2030-01-01",
            headers=_headers(admin_token, "req_prd087_materialized_report"),
        )
        assert report.status_code == 200, report.text
        assert report.json()["total"] == 3
        assert report.json()["cursor"]["freshness_status"] == "current"

    event_claim = claim_next_partner_event(app.state.config, worker_id="event-test", now=current)
    assert event_claim is not None
    renewed_expiries: list[datetime] = []

    def successful_dispatch(*_args, **kwargs):
        for _ in range(2):
            kwargs["source_lease_guard"]()
            renewed_expiries.append(event_claim.lease_expires_at)
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", successful_dispatch)
    assert dispatch_claimed_partner_event(app.state.config, event_claim, now=current)
    assert renewed_expiries == [
        current + timedelta(seconds=91),
        current + timedelta(seconds=92),
    ]
    connection = connect_database(database_url)
    try:
        sent = connection.execute(
            """
            SELECT status,subscription_snapshot_json,snapshot_frozen_at
            FROM partner_customer_outbox WHERE tenant_id=?
            """,
            (tenant_id,),
        ).fetchone()
        assert sent["status"] == "sent"
        assert sent["subscription_snapshot_json"] == "[]"
        assert sent["snapshot_frozen_at"] == "2030-01-01T00:00:00Z"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
