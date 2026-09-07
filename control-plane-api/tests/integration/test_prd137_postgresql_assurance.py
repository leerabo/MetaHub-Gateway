"""Native assessment orchestration only; controlled runners do not test isolation."""

from __future__ import annotations

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd137_persistent_leases as persistent_leases
import test_prd137_security_assurance as assurance

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", [
    assurance.test_get_assessment_supports_reused_request_connection,
    assurance.test_missing_runner_fails_closed,
    assurance.test_fixed_runner_response_idempotency_and_arbitrary_input_rejection,
    assurance.test_finding_requires_new_generation_and_independent_verifier,
    assurance.test_workspace_dual_approval_platform_review_and_expiry,
    assurance.test_permission_revision_change_fails_late_runner_results,
    assurance.test_stale_runner_is_fenced_after_lease_takeover,
    assurance.test_signed_cleanup_failure_prevents_assessment_pass,
], ids=lambda scenario: scenario.__name__.removeprefix("test_"))
async def test_postgresql_orchestration(native_business_database, monkeypatch, tmp_path, scenario):
    runtime = assurance._runtime(
        assurance._config(native_business_database), tmp_path / "receipt-public-trust.json",
    )
    await scenario(runtime, monkeypatch)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["bare_results", "tampered", "wrong_binding", "trust_removed"])
async def test_postgresql_untrusted_receipt(native_business_database, monkeypatch, tmp_path, mode):
    runtime = assurance._runtime(
        assurance._config(native_business_database), tmp_path / "receipt-public-trust.json",
    )
    await assurance.test_untrusted_runner_cannot_pass_or_claim_cleanup(runtime, monkeypatch, mode)


@pytest.mark.anyio
async def test_postgresql_persistent_leases(
    native_business_database, monkeypatch, tmp_path, record_property,
):
    runtime = assurance._runtime(
        assurance._config(native_business_database), tmp_path / "receipt-public-trust.json",
    )
    await persistent_leases.test_persistent_short_lease_controls(
        runtime, monkeypatch, record_property,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["revoked_before_start", "abandoned_queue", "late_start"])
async def test_postgresql_queued_recovery(native_business_database, monkeypatch, tmp_path, mode):
    runtime = assurance._runtime(
        assurance._config(native_business_database), tmp_path / "receipt-public-trust.json",
    )
    await persistent_leases.test_queued_assessment_recovery(runtime, monkeypatch, mode)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["tenant_disabled", "workspace_inactive", "login_disabled"])
async def test_postgresql_owner_revocation(native_business_database, monkeypatch, tmp_path, mode):
    runtime = assurance._runtime(
        assurance._config(native_business_database), tmp_path / "receipt-public-trust.json",
    )
    await persistent_leases.test_owner_and_login_revocation(runtime, monkeypatch, mode)


@pytest.mark.anyio
async def test_postgresql_generation_only_takeover(native_business_database, monkeypatch, tmp_path):
    runtime = assurance._runtime(
        assurance._config(native_business_database), tmp_path / "receipt-public-trust.json",
    )
    await persistent_leases.test_generation_only_takeover(runtime, monkeypatch)


@pytest.mark.anyio
async def test_postgresql_competing_schedulers(native_business_database, monkeypatch, tmp_path):
    runtime = assurance._runtime(
        assurance._config(native_business_database), tmp_path / "receipt-public-trust.json",
    )
    await persistent_leases.test_queued_recovery_competing_schedulers(runtime, monkeypatch)
