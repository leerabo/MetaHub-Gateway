"""Real Workspace key creation and Gateway selection use resolved encryption config."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from metahub_control.providers import routes
from metahub_gateway.cache_affinity.keypool import _select_key_in_transaction
from metahub_gateway.cache_affinity.routing import _binding_target_is_selectable
from metahub_gateway.providers.keypool import select_provider_key
from metahub_gateway.routing.models import GatewayRoute
from metahub_shared.db.security import decrypt_provider_key
from metahub_shared.runtime.errors import ApiError
from test_prd147_workspace_files import _headers, file_subjects, migrated_baseline  # noqa: F401


@pytest.mark.anyio
async def test_workspace_key_and_gateway_share_explicit_encryption_config(
    file_subjects, monkeypatch, caplog,  # noqa: F811
):
    app, database, fixtures = file_subjects
    app.include_router(routes.router)
    secret = "synthetic-provider-configuration-test-master-147"
    plain_key = "synthetic-upstream-test-credential-147"
    app.state.config = app.state.config.model_copy(update={
        "provider_key_encryption_secret": secret,
    })
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", "wrong-process-master-147")
    provider_response = await fixtures.source.post(
        "/providers", headers=_headers(fixtures.source),
        json={"provider_code": "configured_key", "name": "Configuration test",
              "base_url": "https://provider.example.test/v1"},
    )
    assert provider_response.status_code == 201
    provider = provider_response.json()["provider"]
    response = await fixtures.source.post(
        f"/providers/{provider['provider_id']}/keys", headers=_headers(fixtures.source),
        json={"name": "Configured credential", "plain_value": plain_key},
    )
    assert response.status_code == 201
    assert plain_key not in response.text and secret not in response.text
    with closing(sqlite3.connect(database)) as connection:
        ciphertext = connection.execute(
            "SELECT encrypted_key FROM provider_credentials WHERE provider_credential_id=?",
            (response.json()["provider_key"]["provider_key_id"],),
        ).fetchone()[0]
    assert plain_key not in ciphertext and ciphertext.startswith("aesgcm-v1:")
    assert decrypt_provider_key(ciphertext, encryption_secret=secret) == plain_key
    with pytest.raises(ValueError):
        decrypt_provider_key(ciphertext)
    route = GatewayRoute(
        tenant_id=provider["tenant_id"], model_id="configuration-test-model",
        model_alias="configuration-test", upstream_model_name="configuration-test",
        capabilities={"chat": True}, provider_id=provider["provider_id"],
        provider_code=provider["provider_code"], provider_type=provider["provider_type"],
        provider_auth_type=provider["auth_type"], provider_name=provider["name"],
        provider_base_url=provider["base_url"],
    )
    selected = select_provider_key(app.state.config, route)
    assert selected.source == "database" and selected.plain_value == plain_key
    binding = {"provider_credential_id": selected.provider_credential_id,
               "provider_id": route.provider_id, "tenant_id": route.tenant_id,
               "target_kind": "managed"}
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        affinity_key, kind = _select_key_in_transaction(
            app.state.config, connection, route=route, binding=binding,
            request_id="req_configuration_test", trace_id="trc_configuration_test",
        )
        assert kind == "managed" and affinity_key.plain_value == plain_key
        assert _binding_target_is_selectable(
            app.state.config, connection, binding,
            candidate=SimpleNamespace(provider_id=route.provider_id), as_of=datetime.now(UTC),
        )
    with pytest.raises(ApiError) as error:
        select_provider_key(app.state.config.model_copy(update={
            "provider_key_encryption_secret": "wrong-explicit-master-147",
        }), route)
    assert error.value.code == "provider_key.unavailable"
    assert plain_key not in caplog.text and secret not in caplog.text
