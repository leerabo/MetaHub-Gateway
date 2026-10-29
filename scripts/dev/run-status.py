"""Run the local status publisher/read plane without changing Gateway KMS configuration."""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "shared-sdk/python"),
    str(ROOT / "gateway-data-plane/src"),
    str(ROOT / "control-plane-api/src"),
]


def main() -> None:
    import uvicorn
    from metahub_control.public_status.scheduler import (
        install_status_publication_scheduler,
        refresh_status_bundle_once,
    )
    from metahub_shared.runtime.config import load_app_config
    from metahub_shared.status_bundle import test_kms_status_public_key
    from metahub_status.app import StatusPageConfig, create_status_app

    config = load_app_config("control-plane-api")
    if config.environment != "dev":
        raise RuntimeError("Local status launcher requires the dev environment")
    key_path = ROOT / ".data/local-status-signing.key"
    key_path.parent.mkdir(exist_ok=True)
    try:
        with key_path.open("x", encoding="ascii") as handle:
            handle.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    key = key_path.read_text(encoding="ascii").strip()
    if len(key) < 43:
        raise RuntimeError("Invalid local status signing key")
    local_config = config.model_copy(update={"kms_provider": "test", "kms_test_master_key": key})
    refresh_status_bundle_once(local_config)
    app = create_status_app(
        StatusPageConfig(
            bundle_root=Path(config.status_bundle_root).resolve(),
            trusted_keys={"test-v1": test_kms_status_public_key(key)},
        )
    )
    app.state.config = local_config
    install_status_publication_scheduler(app)
    uvicorn.run(app, host="0.0.0.0", port=8002, access_log=False)


if __name__ == "__main__":
    main()
