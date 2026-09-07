"""Actual auth and business routes, in-process; not an isolated deployment attestation."""

from __future__ import annotations

import json
import os
import secrets
import shutil
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_assurance_runner.probes import (
    GLOBAL_CSRF,
    IMPLEMENTED_SCENARIOS,
    WORKSPACE_CSRF,
    FixedProbeRunner,
    ProbeFixtures,
)
from metahub_control.identity.delivery import pop_test_identity_challenge
from metahub_control.identity.routes import router as identity_router
from metahub_control.projects.routes import router as projects_router
from metahub_control.workspace_access.routes import router as access_router
from metahub_control.workspaces.routes import router as workspaces_router
from metahub_shared.db import apply_migrations, apply_seed_data
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "https://assurance.test"


@pytest.fixture(scope="module")
def migrated_baseline(tmp_path_factory):
    supplied = os.environ.get("METAHUB_TEST_MIGRATED_BASELINE")
    if supplied:
        baseline = Path(supplied).resolve(strict=True)
        assert baseline.is_file()
        current = tmp_path_factory.mktemp("prd137-current-probe-base") / "baseline.sqlite3"
        shutil.copy2(baseline, current)
        apply_migrations(f"sqlite:///{current.as_posix()}")
        return current
    baseline = tmp_path_factory.mktemp("prd137-probe-base") / "baseline.sqlite3"
    url = f"sqlite:///{baseline.as_posix()}"
    apply_migrations(url)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(url)
    return baseline


@pytest.fixture
def runtime(migrated_baseline, tmp_path, monkeypatch):
    database = tmp_path / "probe.sqlite3"
    shutil.copy2(migrated_baseline, database)
    url = f"sqlite:///{database.as_posix()}"
    assert not apply_migrations(url).applied
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": url,
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
        "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE": "test_capture",
        "METAHUB_WORKSPACE_ALLOWED_REGIONS": "cn-north-1",
        "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED": "false",
    }
    for key in (
        "AUTH_TOKEN",
        "IDENTITY_SESSION",
        "IDENTITY_CHALLENGE",
        "IDENTITY_PII_ENCRYPTION",
        "WORKSPACE_SESSION",
    ):
        settings[f"METAHUB_{key}_SECRET"] = secrets.token_urlsafe(48)
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    app = create_service_app(
        "control-plane-api",
        "PRD-137 real-auth probe integration",
        extra_cors_allow_headers=("If-Match", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    for router in (identity_router, access_router, workspaces_router, projects_router):
        app.include_router(router)
    assert not app.dependency_overrides
    return app


def _headers(client=None, cookie=GLOBAL_CSRF):
    result = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"}
    if client is not None:
        token = client.cookies.get(cookie)
        assert token
        result["X-CSRF-Token"] = token
    return result


def _body(response, status, stage):
    assert response.status_code == status, f"{stage}: HTTP {response.status_code}"
    return response.json()


async def _register(client, label, password, invitation=None, email=None):
    email = email or f"assurance-{label}-{uuid4().hex}@example.test"
    payload = {
        "email": email,
        "display_name": f"Assurance {label}",
        "password": password,
        "consent_version": "privacy-2026-08",
    }
    if invitation is not None:
        payload["invitation_token"] = invitation
    started = _body(
        await client.post(
            "/identity/registrations",
            json=payload,
            headers=_headers(),
        ),
        200,
        "registration",
    )
    challenge = pop_test_identity_challenge("registration", started["intent_id"])
    assert challenge is not None
    # The mailbox adapter delivers the real generated challenge, not an auth bypass.
    _body(
        await client.post(
            "/identity/verifications",
            json={"intent_id": started["intent_id"], "code": challenge.code},
            headers=_headers(),
        ),
        200,
        "verification",
    )
    _body(
        await client.post(
            "/identity/login",
            json={"email": email, "password": password},
            headers=_headers(),
        ),
        200,
        "login",
    )
    return email


async def _switch(client, workspace_id):
    return _body(
        await client.post(
            "/identity/workspace-switches",
            json={"workspace_id": workspace_id},
            headers=_headers(client),
        ),
        200,
        "workspace switch",
    )


async def _workspace_and_project(client, label):
    workspace = _body(
        await client.post(
            "/workspaces",
            json={
                "kind": "organization",
                "name": f"Assurance {label}",
                "slug": f"assurance-{uuid4().hex}",
                "region": "cn-north-1",
                "service_mode": "internal_only",
            },
            headers={**_headers(client), "Idempotency-Key": uuid4().hex},
        ),
        201,
        "workspace creation",
    )["workspace"]
    await _switch(client, workspace["workspace_id"])
    project = _body(
        await client.post(
            "/projects",
            json={"project_code": f"probe-{label}", "name": f"Probe {label}"},
            headers=_headers(client, WORKSPACE_CSRF),
        ),
        201,
        "project creation",
    )["project"]
    return workspace, project


@pytest.mark.anyio
async def test_fixed_probes_with_registered_accounts_and_real_authorization(
    runtime,
    caplog,
    record_property,
):
    password = "Pr0be!" + secrets.token_urlsafe(24)
    async with AsyncExitStack() as stack:
        clients = [
            await stack.enter_async_context(
                AsyncClient(
                    transport=ASGITransport(app=runtime),
                    base_url=ORIGIN,
                    follow_redirects=False,
                )
            )
            for _ in range(4)
        ]
        source, target, member, anonymous = clients
        await _register(source, "source", password)
        await _register(target, "target", password)
        source_workspace, source_project = await _workspace_and_project(source, "source")
        target_workspace, target_project = await _workspace_and_project(target, "target")
        application = _body(
            await target.post(
                "/applications",
                json={
                    "project_id": target_project["project_id"],
                    "application_code": "probe-target",
                    "name": "Probe target application",
                },
                headers=_headers(target, WORKSPACE_CSRF),
            ),
            201,
            "application creation",
        )["application"]
        member_email = f"assurance-member-{uuid4().hex}@example.test"
        invitation = _body(
            await source.post(
                "/membership-invitations",
                json={"email": member_email, "role": "admin", "expires_in_hours": 24},
                headers=_headers(source, WORKSPACE_CSRF),
            ),
            201,
            "invitation",
        )["accept_token"]
        # Invitation consumption requires the exact invited address.
        await _register(
            member,
            "member",
            password,
            invitation=invitation,
            email=member_email,
        )
        await _switch(member, source_workspace["workspace_id"])
        memberships = _body(await member.get("/memberships"), 200, "memberships")
        membership = next(item for item in memberships["memberships"] if item["is_current"])
        fixtures = ProbeFixtures(
            source=source,
            target=target,
            member=member,
            anonymous=anonymous,
            origin=ORIGIN,
            source_workspace_id=source_workspace["workspace_id"],
            target_workspace_id=target_workspace["workspace_id"],
            source_project_id=source_project["project_id"],
            target_project_id=target_project["project_id"],
            target_project_name=target_project["name"],
            target_application_id=application["application_id"],
            member_membership_id=membership["membership_id"],
            member_revision=membership["revision"],
        )
        lease_checks = 0

        async def test_lease_guard():
            nonlocal lease_checks
            lease_checks += 1
            return True

        runner = FixedProbeRunner(
            fixtures,
            test_lease_guard,
            datetime.now(UTC) + timedelta(minutes=5),
        )
        results = [await runner.run(code) for code in IMPLEMENTED_SCENARIOS]
        serialized = json.dumps([asdict(result) for result in results], default=str)
        record_property("probe_results", serialized)
        assert lease_checks > 50
        sensitive = [password, invitation]
        sensitive.extend(cookie.value for client in clients for cookie in client.cookies.jar)
        assert all(value not in serialized and value not in caplog.text for value in sensitive)
        assert all(result.outcome == "passed" for result in results), [
            (
                result.scenario_code,
                result.outcome,
                result.reason_code,
                [check.code for check in result.checks if not check.satisfied],
            )
            for result in results
        ]
