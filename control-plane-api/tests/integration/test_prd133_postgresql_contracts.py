"""Native public contract/SDK publication, download, and deprecation lifecycle."""

from __future__ import annotations

import hashlib
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd133_developer_contracts as contracts
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_control.developer_contracts.crypto import ExternalSignature
from metahub_control.supply_chain_security import service as supply_chain_service
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


@pytest.mark.anyio
async def test_postgresql_release_download_deprecation_and_withdrawal(
    native_business_database, monkeypatch,
):
    runtime = contracts._runtime(contracts._config(native_business_database))
    await contracts.test_release_download_deprecation_scheduler_and_withdrawal(
        runtime=runtime, monkeypatch=monkeypatch,
    )


@pytest.mark.parametrize("purpose", ["contract", "supply_chain"])
def test_postgresql_signing_key_reuse_cannot_replace_trust(native_business_database, purpose):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    signature = ExternalSignature(
        key_id="g6-test-key-v1", public_key=public, signature=private.sign(b"g6-key-test"),
    )
    if purpose == "contract":
        store_key = contracts.service._store_signing_key
        table = "developer_contract_signing_keys"
        error_code = "developer_contract.kms_unavailable"
    else:
        store_key = supply_chain_service._store_key
        table = "supply_chain_signing_keys"
        error_code = "supply_chain.kms_unavailable"
    now = datetime.now(UTC)
    with closing(connect_database(native_business_database)) as connection:
        store_key(connection, signature, now)
        store_key(connection, signature, now)
        connection.commit()
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
        different = replace(
            signature, public_key=Ed25519PrivateKey.generate().public_key().public_bytes_raw(),
        )
        with pytest.raises(ApiError) as failure:
            store_key(connection, different, now)
        assert failure.value.code == error_code
        connection.rollback()
        assert connection.execute(
            f"SELECT public_key_sha256 FROM {table} WHERE key_id=?", (signature.key_id,),
        ).fetchone()[0] == hashlib.sha256(public).hexdigest()
