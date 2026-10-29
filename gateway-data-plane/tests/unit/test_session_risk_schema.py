"""Preflight must preserve partial-migration and active-freeze boundaries."""

import sqlite3
from types import SimpleNamespace

import pytest
from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.session_risk.service import preflight_session_risk
from metahub_shared.runtime.errors import ApiError


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("score_table", "enabled", "freeze_owner", "denied"),
    [(False, True, None, True), (True, False, None, False),
     (True, False, "own", True), (True, False, "other", False)],
)
async def test_preflight_schema_and_freeze_boundary(
    tmp_path, score_table, enabled, freeze_owner, denied,
):
    path = tmp_path / "risk.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE session_risk_freezes(tenant_id,virtual_key_id,status)")
        if score_table:
            connection.execute("CREATE TABLE session_risk_scores(score_id)")
        if freeze_owner:
            connection.execute(
                "INSERT INTO session_risk_freezes VALUES(?, 'key', 'active')",
                ("own" if freeze_owner == "own" else "other",),
            )
    config = SimpleNamespace(database_url=f"sqlite:///{path.as_posix()}",
                             session_risk_enabled=enabled)
    auth = GatewayAuthContext(tenant_id="own", virtual_key_id="key", key_prefix="mhv_test",
                              environment="dev", environment_id="env")
    if denied:
        with pytest.raises(ApiError) as error:
            await preflight_session_risk(config, SimpleNamespace(), auth, ())
        assert error.value.code == "gateway.session_risk_unavailable"
    else:
        result = await preflight_session_risk(config, SimpleNamespace(), auth, ())
        assert result.status == "disabled"
