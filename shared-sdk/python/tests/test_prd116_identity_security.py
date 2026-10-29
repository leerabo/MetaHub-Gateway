"""PRD-116 shared conditional-access RBAC contracts."""

from __future__ import annotations

from metahub_shared.rbac import (
    CATALOG_REVISION,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)


def test_prd116_conditional_access_role_boundaries_are_exact() -> None:
    assert CATALOG_REVISION == 27
    assert "conditional_access.read" in ROLE_GRANTABLE_PERMISSIONS["viewer"]
    assert "conditional_access.manage" not in ROLE_GRANTABLE_PERMISSIONS["viewer"]
    assert {
        "conditional_access.read",
        "conditional_access.manage",
    }.issubset(ROLE_GRANTABLE_PERMISSIONS["security_admin"])
    assert {
        "conditional_access.read",
        "conditional_access.manage",
    }.issubset(ROLE_GRANTABLE_PERMISSIONS["admin"])
    assert "conditional_access.read" not in ROLE_GRANTABLE_PERMISSIONS["developer"]
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["admin"]) == (
        "89276198685d1c9db600730f0087ed6a24f7b79980493a8babd229922629155b"
    )
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["viewer"]) == (
        "be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af"
    )
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["security_admin"]) == (
        "7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb"
    )
