from __future__ import annotations

import json
import sys

import pytest
from metahub_gateway.local_secrets import (
    LocalKeyMaterialStore,
    LocalKeyStoreError,
    WindowsDpapiProtector,
)
from metahub_gateway.local_secrets import (
    TestKmsProtector as LocalTestKmsProtector,
)

DEPLOYMENT_ID = "edp_" + "1" * 32


def _protector() -> LocalTestKmsProtector:
    return LocalTestKmsProtector("k" * 64, DEPLOYMENT_ID)


def test_keystore_persists_only_protected_independent_roots(tmp_path) -> None:
    path = tmp_path / "keys" / "local-keystore.json"
    store = LocalKeyMaterialStore(path, _protector())
    created = store.load_or_create()
    loaded = store.load_or_create()
    assert loaded.keyring.current.key == created.keyring.current.key
    assert loaded.session_signing_key == created.session_signing_key
    assert loaded.session_signing_key != loaded.keyring.current.key
    assert "session_signing_key=" not in repr(loaded)
    assert "keyring=" not in repr(loaded)

    raw = path.read_bytes()
    assert created.keyring.current.key not in raw
    assert created.session_signing_key not in raw
    payload = json.loads(raw)
    assert set(payload) == {
        "format",
        "current_kek_version",
        "current_kek_blob",
        "previous_kek_version",
        "previous_kek_blob",
        "session_signing_blob",
    }


def test_keystore_tamper_and_cross_deployment_fail_closed(tmp_path) -> None:
    path = tmp_path / "local-keystore.json"
    LocalKeyMaterialStore(path, _protector()).load_or_create()
    raw = bytearray(path.read_bytes())
    raw[-20] = ord("A") if raw[-20] != ord("A") else ord("B")
    path.write_bytes(raw)
    with pytest.raises(LocalKeyStoreError, match="integrity_failed"):
        LocalKeyMaterialStore(path, _protector()).load()

    second_path = tmp_path / "second.json"
    LocalKeyMaterialStore(second_path, _protector()).load_or_create()
    cross = LocalTestKmsProtector("k" * 64, "edp_" + "2" * 32)
    with pytest.raises(LocalKeyStoreError, match="integrity_failed"):
        LocalKeyMaterialStore(second_path, cross).load()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI acceptance")
def test_windows_dpapi_round_trip_is_context_bound(tmp_path) -> None:
    path = tmp_path / "dpapi.json"
    first = LocalKeyMaterialStore(
        path, WindowsDpapiProtector(DEPLOYMENT_ID)
    ).load_or_create()
    second = LocalKeyMaterialStore(path, WindowsDpapiProtector(DEPLOYMENT_ID)).load()
    assert second.keyring.current.key == first.keyring.current.key
    assert second.session_signing_key == first.session_signing_key
    with pytest.raises(LocalKeyStoreError, match="integrity_failed"):
        LocalKeyMaterialStore(
            path, WindowsDpapiProtector("edp_" + "3" * 32)
        ).load()
