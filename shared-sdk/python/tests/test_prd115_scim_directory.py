"""PRD-115 shared SCIM normalization, role, RBAC, and error contracts."""

from __future__ import annotations

import pytest
from metahub_shared.rbac import (
    CATALOG_REVISION,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.scim_directory import (
    choose_lowest_risk_role,
    normalize_scim_external_id,
    normalize_scim_user_name,
    parse_scim_filter,
    parse_scim_version,
    scim_hmac,
)


def test_prd115_scim_normalization_and_filter_subset() -> None:
    assert normalize_scim_user_name("Member@Example.COM") == "member@example.com"
    assert normalize_scim_external_id("workforce/member-001") == "workforce/member-001"
    parsed = parse_scim_filter('userName eq "Member@Example.COM"')
    assert (parsed.attribute, parsed.value) == ("userName", "member@example.com")
    assert parse_scim_version('W/"12"') == 12
    assert parse_scim_version('"12"') == 12
    assert parse_scim_version("12") == 12
    with pytest.raises(ValueError):
        parse_scim_filter('userName eq "member@example.com" or active eq true')
    with pytest.raises(ValueError):
        normalize_scim_user_name("member@localhost")
    with pytest.raises(ValueError):
        parse_scim_version(True)


def test_prd115_hmac_domains_and_conservative_role_merge() -> None:
    secret = "test-prd115-shared-hmac-secret-0000000001"
    token_digest = scim_hmac(secret, "token", "same-value")
    external_digest = scim_hmac(secret, "external_id", "same-value")
    assert len(token_digest) == 64
    assert token_digest != external_digest
    assert choose_lowest_risk_role(["admin", "viewer", "security_admin"]) == "viewer"
    assert choose_lowest_risk_role([]) is None
    with pytest.raises(ValueError):
        choose_lowest_risk_role(["owner"])
    with pytest.raises(ValueError):
        choose_lowest_risk_role(["service_account"])


def test_prd115_rbac_matrix_and_error_registry_are_exact() -> None:
    assert CATALOG_REVISION >= 26
    assert "scim.read" in ROLE_GRANTABLE_PERMISSIONS["viewer"]
    assert "scim.manage" not in ROLE_GRANTABLE_PERMISSIONS["viewer"]
    assert {"scim.read", "scim.manage"}.issubset(
        ROLE_GRANTABLE_PERMISSIONS["security_admin"]
    )
    assert {"scim.read", "scim.manage"}.issubset(ROLE_GRANTABLE_PERMISSIONS["admin"])
    prd115_admin = ROLE_GRANTABLE_PERMISSIONS["admin"] - {
        "conditional_access.read",
        "conditional_access.manage",
    }
    prd115_viewer = ROLE_GRANTABLE_PERMISSIONS["viewer"] - {"conditional_access.read"}
    prd115_security = ROLE_GRANTABLE_PERMISSIONS["security_admin"] - {
        "conditional_access.read",
        "conditional_access.manage",
    }
    assert permission_matrix_sha256(prd115_admin) == (
        "8cc3648fe99882833fd2cfef2b46fd76621f98a264501edae469591704e4d5a8"
    )
    assert permission_matrix_sha256(prd115_viewer) == (
        "e5953a49b667be67bd12d1ae8e94168f49a38644e2f81d2f8f64c655f3890e88"
    )
    assert permission_matrix_sha256(prd115_security) == (
        "f31656fea127e6a06ddd65af47ceab41a9e6311acd5e3e461ce74412a9a95075"
    )
    for code in {
        "scim.request_invalid",
        "scim.origin_invalid",
        "scim.not_found",
        "scim.token_conflict",
        "scim.revision_required",
        "scim.revision_conflict",
        "scim.idempotency_invalid",
        "scim.idempotency_conflict",
        "scim.conflict",
        "scim.unavailable",
    }:
        assert code in ERROR_SPECS
