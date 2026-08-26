from __future__ import annotations

from fastapi import FastAPI
from metahub_gateway.local_secrets import install_local_secret_runtime
from metahub_shared.runtime.config import load_app_config


def test_runtime_gate_is_side_effect_free_when_disabled(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("METAHUB_LOCAL_SECRET_MANAGEMENT_ENABLED", "false")
    app = FastAPI()
    app.state.config = load_app_config("gateway-data-plane")
    install_local_secret_runtime(app)
    assert not hasattr(app.state, "local_secret_store")
    assert list(tmp_path.iterdir()) == []


def test_runtime_assembles_customer_kms_keystore_and_independent_database(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "custody" / "local-secrets.sqlite3"
    keystore_path = tmp_path / "keys" / "local-keystore.json"
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_LOCAL_SECRET_MANAGEMENT_ENABLED", "true")
    monkeypatch.setenv("METAHUB_LOCAL_SECRET_DEPLOYMENT_ID", "edp_" + "1" * 32)
    monkeypatch.setenv(
        "METAHUB_LOCAL_SECRET_ADMIN_HANDOFF_SECRET", "h" * 48
    )
    monkeypatch.setenv("METAHUB_LOCAL_SECRET_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("METAHUB_LOCAL_SECRET_KEYSTORE_PATH", str(keystore_path))
    monkeypatch.setenv("METAHUB_KMS_PROVIDER", "test")
    monkeypatch.setenv("METAHUB_KMS_TEST_MASTER_KEY", "m" * 64)

    app = FastAPI()
    app.state.config = load_app_config("gateway-data-plane")
    install_local_secret_runtime(app)
    assert app.state.local_secret_store.path == database_path.resolve()
    assert database_path.exists()
    assert keystore_path.exists()
    assert "target_count=" in repr(app.state.local_provider_tester)
    assert "key=" not in repr(app.state.local_admin_session_signer)
    assert repr(app.state.local_admin_handoff_verifier) == "LocalAdminHandoffVerifier()"
    dumped = str(app.state.config.model_dump())
    assert str(database_path) not in dumped
    assert str(keystore_path) not in dumped
    assert "m" * 64 not in dumped
    assert "h" * 48 not in dumped
