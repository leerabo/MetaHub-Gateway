"""PRD-138 signed gates, independent decisions, waves, rollback, and RC boundary."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import shutil
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from metahub_control.launch_governance import routes, service
from metahub_control.launch_governance.routes import router as launch_router
from metahub_control.launch_governance.verifier import install_launch_evidence_verifier
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
PLATFORM_CSRF = "c" * 43
STEP_UP = "u" * 43
GATE_CODES = (
    "api",
    "bb",
    "comp",
    "compliance",
    "disaster_recovery",
    "edge",
    "flow",
    "gr",
    "long_stability",
    "migration",
    "payment",
    "perf",
    "privacy",
    "sec",
    "supply_chain",
    "tenant_isolation",
    "ucd",
    "ui",
    "wb",
    "web",
)
WAVES = (
    "internal",
    "personal_pilot",
    "enterprise_pilot",
    "managed_private_pilot",
)
ACTORS = {
    "product": "product",
    "sre": "sre",
    "security": "security",
    "test": "test",
    "finance": "finance",
    "legal": "legal",
}
ROLE_BY_NAME = {
    "requester": "release_manager",
    "product": "platform_owner",
    "sre": "platform_ops",
    "security": "platform_security",
    "test": "platform_verification",
    "finance": "platform_billing",
    "legal": "platform_compliance",
    "executor": "platform_owner",
    "rollback_requester": "platform_owner",
    "rollback_verifier": "platform_owner",
}


@dataclass(frozen=True, slots=True)
class Runtime:
    config: AppConfig
    workspace_id: str
    tenant_id: str
    tokens: dict[str, str]
    contexts: dict[str, PlatformContext]
    operator_ids: dict[str, str]


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _config(database_url: str) -> AppConfig:
    return AppConfig(
        service_name="control-plane-api",
        environment="test",
        environment_info=EnvironmentInfo(
            name="test",
            is_production=False,
            config_profile="test",
            service_mode="internal_only",
            loaded_at=datetime.now(UTC),
        ),
        auth_token_secret=secrets.token_urlsafe(32),
        identity_session_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        support_session_secret=secrets.token_urlsafe(32),
        approval_hmac_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd138-api-base") / "base.sqlite3"
    database_url = _url(database)
    applied = apply_migrations(database_url).applied
    assert "0130" in applied
    assert apply_migrations(database_url).applied == []
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(base_database: Path, tmp_path: Path) -> Runtime:
    database = tmp_path / "prd138.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    stamp = "2026-08-31T00:00:00.000Z"
    tokens: dict[str, str] = {}
    contexts: dict[str, PlatformContext] = {}
    operator_ids: dict[str, str] = {}
    with closing(connect_database(config.database_url)) as connection:
        owner = connection.execute(
            """SELECT membership.workspace_id,membership.tenant_id
                 FROM workspace_memberships membership
                WHERE membership.role_code='owner' AND membership.status='active' LIMIT 1"""
        ).fetchone()
        assert owner is not None
        for index, (name, role) in enumerate(ROLE_BY_NAME.items(), start=1):
            marker = format(index, "x")
            account_id = "gac_" + marker * 32
            operator_id = "pto_" + marker * 32
            token = chr(96 + index) * 43
            connection.execute(
                """INSERT INTO global_accounts(
                  account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                  credential_revision,session_revision,mfa_state,failed_login_count,
                  locked_until,last_login_at,created_at,updated_at)
                VALUES(?,?,NULL,?,'active',0,1,1,'not_enrolled',0,NULL,NULL,?,?)""",
                (account_id, f"prd138-{name}@example.invalid", name, stamp, stamp),
            )
            connection.execute(
                """INSERT INTO platform_operators(
                  operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
                VALUES(?,?,'active',1,NULL,?,?)""",
                (operator_id, account_id, stamp, stamp),
            )
            connection.execute(
                """INSERT INTO platform_operator_role_bindings(
                  operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                VALUES(?,?,'active',NULL,?,NULL)""",
                (operator_id, role, stamp),
            )
            tokens[name] = token
            operator_ids[name] = operator_id
        connection.commit()
        expires = datetime.now(UTC) + timedelta(hours=1)
        for name, token in tokens.items():
            operator_id = operator_ids[name]
            roles, permissions, _fingerprint = platform_access_service._authority(
                connection, operator_id
            )
            marker = operator_id[4]
            contexts[token] = PlatformContext(
                platform_session_id="pts_" + marker * 32,
                operator_id=operator_id,
                account_id="gac_" + marker * 32,
                global_session_id="gss_" + marker * 32,
                factor_id="mfa_" + marker * 32,
                factor_revision=1,
                operator_revision=1,
                roles=roles,
                permissions=permissions,
                expires_at=expires,
            )
    return Runtime(
        config=config,
        workspace_id=str(owner["workspace_id"]),
        tenant_id=str(owner["tenant_id"]),
        tokens=tokens,
        contexts=contexts,
        operator_ids=operator_ids,
    )


def _app(monkeypatch: pytest.MonkeyPatch, runtime: Runtime, verifier=True):
    def platform_actor(connection, _config, *, session_token, **_kwargs):
        context = runtime.contexts.get(session_token)
        if context is None:
            raise ApiError("platform.session_required")
        roles, permissions, _fingerprint = platform_access_service._authority(
            connection, context.operator_id
        )
        revision = int(
            connection.execute(
                "SELECT revision FROM platform_operators WHERE operator_id=?",
                (context.operator_id,),
            ).fetchone()[0]
        )
        return replace(context, roles=roles, permissions=permissions, operator_revision=revision)

    monkeypatch.setattr(service, "authenticate_platform_session_in_transaction", platform_actor)
    monkeypatch.setattr(
        service,
        "consume_global_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: "mfa",
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-138 Launch Governance Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Step-Up-Grant",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = runtime.config
    if verifier is not None:
        app.state.launch_evidence_verifier = lambda _request, _signature: verifier
    app.include_router(launch_router)
    return app


def test_prd138_list_supports_reused_request_connection(
    monkeypatch: pytest.MonkeyPatch, runtime: Runtime
) -> None:
    monkeypatch.setattr(
        service,
        "authenticate_platform_session_in_transaction",
        lambda *_args, **_kwargs: runtime.contexts[runtime.tokens["product"]],
    )
    with database_request_scope(reuse_connections=True):
        response = service.list_launch_gates(
            runtime.config,
            platform_session_token=runtime.tokens["product"],
            request_id="req_prd138_reused_connection",
            trace_id="trc_prd138_reused_connection",
        )
    assert len(response.gates) == 20


def _headers(token: str, key: str | None = None, revision: int = 1) -> dict[str, str]:
    headers = {"Cookie": f"{PLATFORM_SESSION_COOKIE}={token}"}
    if key is not None:
        headers = {
            "Cookie": (
                f"{PLATFORM_SESSION_COOKIE}={token}; {PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
            ),
            "Idempotency-Key": key,
            "If-Match": f'"{revision}"',
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-site",
            "X-CSRF-Token": PLATFORM_CSRF,
            "X-Step-Up-Grant": STEP_UP,
        }
    return headers


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _candidate_body(workspace_id: str) -> dict[str, object]:
    artifacts = [
        {
            "gate_code": code,
            "workspace_id": workspace_id,
            "artifact_sha256": _sha(f"artifact:{code}"),
        }
        for code in GATE_CODES
    ]
    waves = [
        {
            "wave_kind": wave,
            "workspace_id": workspace_id,
            "cohort_manifest_sha256": _sha(f"cohort:{wave}"),
            "feature_flag_generation": 1,
            "migration_plan_sha256": _sha(f"migration:{wave}"),
            "rollback_plan_sha256": _sha(f"rollback:{wave}"),
            "observation_seconds": 60,
        }
        for wave in WAVES
    ]
    return {
        "operation": "create",
        "code_tree_sha256": _sha("code"),
        "migration_set_sha256": _sha("migrations"),
        "contract_set_sha256": _sha("contracts"),
        "artifact_set_sha256": _digest(sorted(artifacts, key=lambda item: item["gate_code"])),
        "test_manifest_sha256": _sha("tests"),
        "artifacts": artifacts,
        "waves": waves,
        "confirmed": True,
    }


async def _create_candidate(client: AsyncClient, runtime: Runtime) -> dict[str, object]:
    response = await client.post(
        "/platform/launches",
        headers=_headers(runtime.tokens["requester"], "prd138-create-candidate-0001"),
        json=_candidate_body(runtime.workspace_id),
    )
    assert response.status_code == 201, response.text
    return response.json()["candidate"]


async def _submit_all_evidence(
    client: AsyncClient, runtime: Runtime, candidate: dict[str, object], now: datetime
) -> dict[str, object]:
    gate_response = await client.get(
        "/platform/launch-gates", headers=_headers(runtime.tokens["test"])
    )
    assert gate_response.status_code == 200
    gates = {item["gate_code"]: item for item in gate_response.json()["gates"]}
    current = candidate
    for index, code in enumerate(GATE_CODES):
        started = now - (timedelta(hours=73) if code == "long_stability" else timedelta(minutes=5))
        response = await client.post(
            f"/platform/launch-gates/{gates[code]['launch_gate_id']}/evidence",
            headers=_headers(
                runtime.tokens["test"],
                f"prd138-evidence-{index:02d}-0001",
                current["revision"],
            ),
            json={
                "launch_candidate_id": current["launch_candidate_id"],
                "expected_revision": current["revision"],
                "workspace_id": runtime.workspace_id,
                "artifact_sha256": _sha(f"artifact:{code}"),
                "verifier_code": gates[code]["verifier_code"],
                "verifier_version": 1,
                "outcome": "passed",
                "metric_value": 1.0,
                "metric_unit": "pass_ratio",
                "environment_sha256": _sha(f"environment:{code}"),
                "provenance_sha256": _sha(f"provenance:{code}"),
                "started_at": started.isoformat(),
                "finished_at": now.isoformat(),
                "signature": "s" * 64,
                "confirmed": True,
            },
        )
        assert response.status_code == 201, response.text
        current = response.json()["candidate"]
    assert current["status"] == "signing"
    return current


async def _approve_all(
    client: AsyncClient, runtime: Runtime, candidate: dict[str, object]
) -> dict[str, object]:
    return await _approve_actor_kinds(client, runtime, candidate, tuple(ACTORS))


async def _approve_actor_kinds(
    client: AsyncClient,
    runtime: Runtime,
    candidate: dict[str, object],
    actor_kinds: tuple[str, ...],
) -> dict[str, object]:
    current = candidate
    for actor_kind in actor_kinds:
        token_name = ACTORS[actor_kind]
        response = await client.post(
            "/platform/launches",
            headers=_headers(
                runtime.tokens[token_name],
                f"prd138-decision-{actor_kind}-0001",
                current["revision"],
            ),
            json={
                "operation": "decide",
                "launch_candidate_id": current["launch_candidate_id"],
                "expected_revision": current["revision"],
                "actor_kind": actor_kind,
                "approved": True,
                "reason_code": f"{actor_kind}_approved",
                "confirmed": True,
            },
        )
        assert response.status_code == 201, response.text
        current = response.json()["candidate"]
    if len(actor_kinds) == len(ACTORS):
        assert current["status"] == "ready"
    return current


@pytest.mark.anyio
async def test_four_routes_strict_body_and_missing_verifier_fail_closed(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(monkeypatch, runtime, verifier=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        gates = await client.get(
            "/platform/launch-gates", headers=_headers(runtime.tokens["requester"])
        )
        assert gates.status_code == 200
        assert len(gates.json()["gates"]) == 20
        candidate = await _create_candidate(client, runtime)
        gate = gates.json()["gates"][0]
        now = datetime.now(UTC)
        failed = await client.post(
            f"/platform/launch-gates/{gate['launch_gate_id']}/evidence",
            headers=_headers(runtime.tokens["test"], "prd138-no-verifier-0001", 1),
            json={
                "launch_candidate_id": candidate["launch_candidate_id"],
                "expected_revision": 1,
                "workspace_id": runtime.workspace_id,
                "artifact_sha256": _sha(f"artifact:{gate['gate_code']}"),
                "verifier_code": gate["verifier_code"],
                "verifier_version": 1,
                "outcome": "passed",
                "metric_value": 1.0,
                "metric_unit": "pass_ratio",
                "environment_sha256": _sha("environment"),
                "provenance_sha256": _sha("provenance"),
                "started_at": (now - timedelta(minutes=1)).isoformat(),
                "finished_at": now.isoformat(),
                "signature": "s" * 64,
                "confirmed": True,
            },
        )
        assert failed.status_code == 503
        assert failed.json()["code"] == "launch.verifier_unavailable"
        arbitrary = await client.post(
            "/platform/launches",
            headers=_headers(runtime.tokens["requester"], "prd138-arbitrary-0001"),
            json={**_candidate_body(runtime.workspace_id), "override": True},
        )
        assert arbitrary.status_code == 400
        assert "general_availability" not in gates.text + arbitrary.text
    methods = {
        (route.path, method) for route in routes.router.routes for method in route.methods or set()
    }
    assert methods == {
        ("/platform/launch-gates", "GET"),
        ("/platform/launch-gates/{launch_gate_id}/evidence", "POST"),
        ("/platform/launches", "POST"),
        ("/platform/launches/{launch_candidate_id}/rollback", "POST"),
    }


@pytest.mark.anyio
@pytest.mark.parametrize("rejection", ["wrong_signature", "removed_key", "invalid_trust"])
async def test_real_signature_api_rejects_without_writes_and_persists_only_digests(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rejection: str
) -> None:
    # Authentication/step-up remain fixture doubles; the signer and verifier are real.
    private = Ed25519PrivateKey.generate()
    trust_path = tmp_path / "deployment-public-trust.json"
    config = runtime.config.model_copy(update={
        "launch_evidence_trust_bundle_path": str(trust_path),
    })
    app = _app(monkeypatch, replace(runtime, config=config), verifier=None)
    install_launch_evidence_verifier(app)
    now = datetime.now(UTC)

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def facts():
        with closing(connect_database(config.database_url)) as connection:
            return {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in (
                    "launch_candidates", "launch_evidence", "launch_events",
                    "launch_event_outbox", "launch_action_commands",
                )
            }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        candidate = await _create_candidate(client, runtime)
        response = await client.get(
            "/platform/launch-gates", headers=_headers(runtime.tokens["test"])
        )
        assert response.status_code == 200
        gate = next(item for item in response.json()["gates"] if item["gate_code"] == "api")
        bundle = {"schema_version": "metahub.launch-evidence-trust.v1", "keys": [{
            "key_id": "api-test", "gate_code": "api", "verifier_code": gate["verifier_code"],
            "verifier_version": gate["verifier_version"],
            "public_key": b64(private.public_key().public_bytes_raw()),
            "not_before": (now - timedelta(hours=1)).isoformat(),
            "not_after": (now + timedelta(hours=1)).isoformat(),
        }]}
        body = {
            "launch_candidate_id": candidate["launch_candidate_id"],
            "expected_revision": candidate["revision"], "workspace_id": runtime.workspace_id,
            "artifact_sha256": _sha("artifact:api"), "verifier_code": gate["verifier_code"],
            "verifier_version": gate["verifier_version"], "outcome": "passed",
            "metric_value": 1.0, "metric_unit": "pass_ratio",
            "environment_sha256": _sha("test-environment"),
            "provenance_sha256": _sha("test-provenance"),
            "started_at": (now - timedelta(minutes=1)).isoformat(),
            "finished_at": now.isoformat(), "confirmed": True,
        }
        # Build the external signer envelope independently of the service helper.
        envelope = {key: value for key, value in body.items()
                    if key not in {"expected_revision", "confirmed"}}
        envelope.update({
            "candidate_sha256": candidate["candidate_sha256"],
            "launch_gate_id": gate["launch_gate_id"], "gate_code": gate["gate_code"],
            "policy_sha256": gate["policy_sha256"], "tenant_id": runtime.tenant_id,
        })
        for key in ("started_at", "finished_at"):
            envelope[key] = datetime.fromisoformat(body[key]).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
        envelope_sha = _digest(envelope)
        signature = b64(private.sign(b"metahub.launch-evidence.v1\0" + bytes.fromhex(envelope_sha)))
        body["signature"] = signature
        trust_path.write_text(json.dumps(bundle), encoding="utf-8")
        if rejection == "wrong_signature":
            body["signature"] = b64(Ed25519PrivateKey.generate().sign(b"wrong signer"))
        elif rejection == "removed_key":
            trust_path.write_text(json.dumps({**bundle, "keys": []}), encoding="utf-8")
        else:
            trust_path.write_text('{"keys":[],"keys":[]}', encoding="utf-8")
        before = facts()
        url = f"/platform/launch-gates/{gate['launch_gate_id']}/evidence"
        headers = _headers(runtime.tokens["test"], "prd138-real-signature-0001")
        rejected = await client.post(url, headers=headers, json=body)
        assert rejected.json()["code"] == "launch.verifier_invalid", rejected.text
        assert rejected.status_code == 403
        assert facts() == before
        assert str(trust_path) not in rejected.text
        trust_path.write_text(json.dumps(bundle), encoding="utf-8")
        body["signature"] = signature
        accepted = await client.post(url, headers=headers, json=body)
        assert accepted.status_code == 201, accepted.text
        assert signature not in accepted.text
        assert accepted.json()["candidate"]["revision"] == candidate["revision"] + 1
        with closing(connect_database(config.database_url)) as connection:
            evidence = connection.execute("SELECT * FROM launch_evidence").fetchall()
        assert len(evidence) == 1
        stored = dict(evidence[0])
        assert stored["envelope_sha256"] == envelope_sha
        assert stored["signature_sha256"] == _sha(signature)
        assert "signature" not in stored
        assert signature not in json.dumps(facts())


@pytest.mark.anyio
async def test_signed_evidence_and_six_independent_decisions_reach_ready(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr(service, "_utcnow", lambda: now)
    app = _app(monkeypatch, runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        candidate = await _create_candidate(client, runtime)
        candidate = await _submit_all_evidence(client, runtime, candidate, now)
        requester_sign = await client.post(
            "/platform/launches",
            headers=_headers(
                runtime.tokens["requester"], "prd138-self-sign-0001", candidate["revision"]
            ),
            json={
                "operation": "decide",
                "launch_candidate_id": candidate["launch_candidate_id"],
                "expected_revision": candidate["revision"],
                "actor_kind": "product",
                "approved": True,
                "reason_code": "self_approval",
                "confirmed": True,
            },
        )
        assert requester_sign.status_code == 403
        candidate = await _approve_all(client, runtime, candidate)
        assert len(candidate["evidence"]) == 20
        assert len(candidate["decisions"]) == 6
        assert len({item["operator_id"] for item in candidate["decisions"]}) == 6


@pytest.mark.anyio
async def test_permission_revision_change_blocks_wave_start(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr(service, "_utcnow", lambda: now)
    app = _app(monkeypatch, runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        candidate = await _approve_all(
            client,
            runtime,
            await _submit_all_evidence(
                client, runtime, await _create_candidate(client, runtime), now
            ),
        )
        with closing(connect_database(runtime.config.database_url)) as connection:
            connection.execute(
                "UPDATE platform_operators SET revision=2,updated_at=? WHERE operator_id=?",
                ((now + timedelta(seconds=1)).isoformat(), runtime.operator_ids["security"]),
            )
            connection.commit()
        blocked = await client.post(
            "/platform/launches",
            headers=_headers(
                runtime.tokens["executor"], "prd138-revoked-start-0001", candidate["revision"]
            ),
            json={
                "operation": "start_wave",
                "launch_candidate_id": candidate["launch_candidate_id"],
                "expected_revision": candidate["revision"],
                "wave_kind": "internal",
                "confirmed": True,
            },
        )
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "launch.permission_revoked"


@pytest.mark.anyio
async def test_final_decision_rechecks_all_current_signer_permissions(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr(service, "_utcnow", lambda: now)
    app = _app(monkeypatch, runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        candidate = await _approve_actor_kinds(
            client,
            runtime,
            await _submit_all_evidence(
                client, runtime, await _create_candidate(client, runtime), now
            ),
            tuple(ACTORS)[:-1],
        )
        with closing(connect_database(runtime.config.database_url)) as connection:
            connection.execute(
                "UPDATE platform_operators SET revision=2,updated_at=? WHERE operator_id=?",
                ((now + timedelta(seconds=1)).isoformat(), runtime.operator_ids["security"]),
            )
            connection.commit()
        blocked = await client.post(
            "/platform/launches",
            headers=_headers(
                runtime.tokens["legal"],
                "prd138-stale-final-sign-0001",
                candidate["revision"],
            ),
            json={
                "operation": "decide",
                "launch_candidate_id": candidate["launch_candidate_id"],
                "expected_revision": candidate["revision"],
                "actor_kind": "legal",
                "approved": True,
                "reason_code": "legal_approved",
                "confirmed": True,
            },
        )
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "launch.permission_revoked"
        current = await client.get(
            "/platform/launch-gates", headers=_headers(runtime.tokens["legal"])
        )
        assert current.status_code == 200
        assert current.json()["candidate"]["status"] == "signing"
        assert len(current.json()["candidate"]["decisions"]) == 5


@pytest.mark.anyio
async def test_four_waves_and_review_can_only_form_release_candidate(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(service, "_utcnow", lambda: clock[0])
    app = _app(monkeypatch, runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        candidate = await _approve_all(
            client,
            runtime,
            await _submit_all_evidence(
                client, runtime, await _create_candidate(client, runtime), clock[0]
            ),
        )
        for index, wave_kind in enumerate(WAVES):
            started = await client.post(
                "/platform/launches",
                headers=_headers(
                    runtime.tokens["executor"],
                    f"prd138-wave-start-{index}-0001",
                    candidate["revision"],
                ),
                json={
                    "operation": "start_wave",
                    "launch_candidate_id": candidate["launch_candidate_id"],
                    "expected_revision": candidate["revision"],
                    "wave_kind": wave_kind,
                    "confirmed": True,
                },
            )
            assert started.status_code == 201, started.text
            candidate = started.json()["candidate"]
            clock[0] += timedelta(seconds=61)
            completed = await client.post(
                "/platform/launches",
                headers=_headers(
                    runtime.tokens["executor"],
                    f"prd138-wave-complete-{index}-0001",
                    candidate["revision"],
                ),
                json={
                    "operation": "complete_wave",
                    "launch_candidate_id": candidate["launch_candidate_id"],
                    "expected_revision": candidate["revision"],
                    "wave_kind": wave_kind,
                    "health_watermark_sha256": _sha(f"health:{wave_kind}"),
                    "confirmed": True,
                },
            )
            assert completed.status_code == 201, completed.text
            candidate = completed.json()["candidate"]
        assert candidate["status"] == "review_pending"
        reviewed = await client.post(
            "/platform/launches",
            headers=_headers(
                runtime.tokens["executor"], "prd138-review-0001", candidate["revision"]
            ),
            json={
                "operation": "review",
                "launch_candidate_id": candidate["launch_candidate_id"],
                "expected_revision": candidate["revision"],
                "summary_sha256": _sha("review-summary"),
                "action_items_sha256": _sha("review-actions"),
                "conclusion": "release_candidate",
                "confirmed": True,
            },
        )
        assert reviewed.status_code == 201, reviewed.text
        result = reviewed.json()["candidate"]
        assert result["status"] == "release_candidate"
        assert result["review"]["conclusion"] == "release_candidate"
        assert "general_availability" not in reviewed.text


@pytest.mark.anyio
async def test_rollback_requires_distinct_verifier_and_previous_wave(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(service, "_utcnow", lambda: clock[0])
    app = _app(monkeypatch, runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        candidate = await _approve_all(
            client,
            runtime,
            await _submit_all_evidence(
                client, runtime, await _create_candidate(client, runtime), clock[0]
            ),
        )
        started = await client.post(
            "/platform/launches",
            headers=_headers(
                runtime.tokens["executor"], "prd138-rollback-start-0001", candidate["revision"]
            ),
            json={
                "operation": "start_wave",
                "launch_candidate_id": candidate["launch_candidate_id"],
                "expected_revision": candidate["revision"],
                "wave_kind": "internal",
                "confirmed": True,
            },
        )
        assert started.status_code == 201
        candidate = started.json()["candidate"]
        requested = await client.post(
            f"/platform/launches/{candidate['launch_candidate_id']}/rollback",
            headers=_headers(
                runtime.tokens["rollback_requester"],
                "prd138-rollback-request-0001",
                candidate["revision"],
            ),
            json={
                "operation": "request",
                "expected_revision": candidate["revision"],
                "from_wave_kind": "internal",
                "to_wave_kind": "pre_launch",
                "trigger_code": "operator_request",
                "trigger_sha256": _sha("trigger"),
                "checkpoint_sha256": _sha("checkpoint"),
                "job_receipt_sha256": _sha("job"),
                "confirmed": True,
            },
        )
        assert requested.status_code == 201, requested.text
        rollback = requested.json()["rollback"]
        self_verify = await client.post(
            f"/platform/launches/{candidate['launch_candidate_id']}/rollback",
            headers=_headers(
                runtime.tokens["rollback_requester"],
                "prd138-rollback-self-verify-0001",
                rollback["revision"],
            ),
            json={
                "operation": "verify",
                "expected_revision": rollback["revision"],
                "launch_rollback_id": rollback["launch_rollback_id"],
                "outcome": "passed",
                "verification_sha256": _sha("verification"),
                "confirmed": True,
            },
        )
        assert self_verify.status_code == 409
        executor_verify = await client.post(
            f"/platform/launches/{candidate['launch_candidate_id']}/rollback",
            headers=_headers(
                runtime.tokens["executor"],
                "prd138-rollback-executor-verify-0001",
                rollback["revision"],
            ),
            json={
                "operation": "verify",
                "expected_revision": rollback["revision"],
                "launch_rollback_id": rollback["launch_rollback_id"],
                "outcome": "passed",
                "verification_sha256": _sha("verification"),
                "confirmed": True,
            },
        )
        assert executor_verify.status_code == 409
        assert executor_verify.json()["code"] == "launch.independence_required"
        verified = await client.post(
            f"/platform/launches/{candidate['launch_candidate_id']}/rollback",
            headers=_headers(
                runtime.tokens["rollback_verifier"],
                "prd138-rollback-verify-0001",
                rollback["revision"],
            ),
            json={
                "operation": "verify",
                "expected_revision": rollback["revision"],
                "launch_rollback_id": rollback["launch_rollback_id"],
                "outcome": "passed",
                "verification_sha256": _sha("verification"),
                "confirmed": True,
            },
        )
        assert verified.status_code == 201, verified.text
        assert verified.json()["candidate"]["status"] == "rolled_back"
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM launch_events WHERE event_type='launch.rolled_back'"
            ).fetchone()[0]
            == 1
        )
