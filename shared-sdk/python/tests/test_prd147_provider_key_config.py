"""Provider encryption must use the same resolved config in both services."""

import pytest
from metahub_shared.runtime.config import config_loaded_log_payload, load_app_config


@pytest.mark.parametrize("service", ["control-plane-api", "gateway-data-plane"])
def test_provider_encryption_secret_from_file_is_private(monkeypatch, tmp_path, service):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.delenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", raising=False)
    secret = "synthetic-provider-file-encryption-configuration-147"
    (tmp_path / ".env").write_text(
        f"METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET={secret}\n", encoding="utf-8",
    )
    config = load_app_config(service)
    assert config.provider_key_encryption_secret == secret
    assert "provider_key_encryption_secret" not in config.model_dump()
    assert secret not in repr(config)
    assert secret not in repr(config_loaded_log_payload(config))
    override = "synthetic-provider-environment-encryption-override-147"
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", override)
    assert load_app_config(service).provider_key_encryption_secret == override
