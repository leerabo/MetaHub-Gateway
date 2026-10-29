from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from metahub_gateway.local_secrets import (
    LocalSecretStore,
    LocalSecretSyncError,
    LocalSecretSyncUploader,
)
from metahub_shared.private_gateway import LocalKek, LocalKekKeyring

DEPLOYMENT_ID = "edp_" + "1" * 32
EDGE_SESSION = "egs_" + "A" * 43
NOW = datetime(2026, 8, 27, 3, 0, 0, tzinfo=UTC)


def _store(tmp_path, clock: list[datetime]) -> LocalSecretStore:
    store = LocalSecretStore(
        tmp_path / "custody" / "local-secrets.sqlite3",
        deployment_id=DEPLOYMENT_ID,
        keyring=LocalKekKeyring(LocalKek(1, bytes(range(32)))),
        now=lambda: clock[0],
    )
    store.create(
        tenant_id="ten_private_001",
        workspace_id="wsp_private_001",
        provider_connection_id="pcn_private_001",
        label="Primary provider credential",
        kind="api_key",
        plaintext=bytearray(range(1, 65)),
    )
    return store


def _receipt(batch: dict[str, object], **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "1",
        "receipt_id": "lsa_" + "2" * 32,
        "batch_id": batch["batch_id"],
        "deployment_id": batch["deployment_id"],
        "disposition": "accepted",
        "accepted_event_count": len(batch["events"]),
        "duplicate_event_count": 0,
        "received_at": "2026-08-27T03:01:00.000Z",
        "request_id": "request-prd123-sync",
        "trace_id": "trace-prd123-sync",
    }
    value.update(overrides)
    return value


def test_sync_retries_byte_identical_batch_and_acks_only_exact_receipt(tmp_path) -> None:
    clock = [NOW]
    store = _store(tmp_path, clock)
    sent: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        assert request.method == "POST"
        assert request.url == httpx.URL(
            "http://localhost:8000/internal/edge/secret-references/sync"
        )
        assert request.headers["content-type"] == "application/json"
        assert request.headers["cache-control"] == "no-store"
        assert request.headers["x-edge-session"] == EDGE_SESSION
        if len(sent) == 1:
            raise httpx.ConnectError("sensitive transport detail", request=request)
        batch = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Cache-Control": "private, no-store"},
            json=_receipt(batch),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        uploader = LocalSecretSyncUploader(client, "http://localhost:8000")
        with pytest.raises(LocalSecretSyncError) as failure:
            uploader.sync(store, edge_session=EDGE_SESSION)
        assert failure.value.code == "local_secret.transport_unavailable"
        assert str(failure.value) == "local_secret.transport_unavailable"
        assert store.pending_sync_event_count() == 1

        clock[0] += timedelta(minutes=1)
        assert uploader.sync(store, edge_session=EDGE_SESSION) == 1

    assert sent[0] == sent[1]
    assert store.pending_sync_event_count() == 0
    connection = sqlite3.connect(store.path)
    try:
        delivered = connection.execute(
            """
            SELECT delivery_batch_id,delivery_attempt_count,delivery_receipt_id,delivered_at
            FROM local_secret_outbox
            """
        ).fetchone()
        assert delivered[0].startswith("lsb_")
        assert delivered[1] == 2
        assert delivered[2] == "lsa_" + "2" * 32
        assert delivered[3] == "2026-08-27T03:01:00.000Z"
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("case", "overrides"),
    [
        ("wrong_batch", {"batch_id": "lsb_" + "9" * 32}),
        ("wrong_owner", {"deployment_id": "edp_" + "9" * 32}),
        ("wrong_count", {"accepted_event_count": 2}),
    ],
)
def test_sync_rejects_mismatched_receipt_without_ack(tmp_path, case, overrides) -> None:
    clock = [NOW]
    store = _store(tmp_path, clock)

    def handler(request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store"},
            json=_receipt(batch, **overrides),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        uploader = LocalSecretSyncUploader(client, "http://localhost:8000")
        with pytest.raises(LocalSecretSyncError) as failure:
            uploader.sync(store, edge_session=EDGE_SESSION)
    assert case
    assert failure.value.code == "local_secret.receipt_invalid"
    assert store.pending_sync_event_count() == 1


@pytest.mark.parametrize(
    "case",
    ["missing_no_store", "wrong_content_type", "duplicate_json_key", "oversized"],
)
def test_sync_rejects_untrusted_receipt_encoding_without_ack(tmp_path, case) -> None:
    clock = [NOW]
    store = _store(tmp_path, clock)

    def handler(request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)
        encoded = json.dumps(_receipt(batch), separators=(",", ":")).encode()
        headers = {"Cache-Control": "no-store", "Content-Type": "application/json"}
        if case == "missing_no_store":
            headers["Cache-Control"] = "private"
        elif case == "wrong_content_type":
            headers["Content-Type"] = "text/plain"
        elif case == "duplicate_json_key":
            encoded = b'{"schema_version":"1",' + encoded[1:]
        elif case == "oversized":
            encoded += b" " * 8_192
        return httpx.Response(200, headers=headers, content=encoded)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        uploader = LocalSecretSyncUploader(client, "http://localhost:8000")
        with pytest.raises(LocalSecretSyncError) as failure:
            uploader.sync(store, edge_session=EDGE_SESSION)
    assert failure.value.code == "local_secret.receipt_invalid"
    assert store.pending_sync_event_count() == 1


def test_sync_rejects_invalid_endpoint_and_session_before_claim(tmp_path) -> None:
    clock = [NOW]
    store = _store(tmp_path, clock)
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(LocalSecretSyncError, match="local_secret.endpoint_invalid"):
            LocalSecretSyncUploader(client, "http://control.example.com")
        uploader = LocalSecretSyncUploader(client, "https://control.example.com")
        with pytest.raises(LocalSecretSyncError, match="local_secret.authentication_failed"):
            uploader.sync(store, edge_session="invalid")

    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute(
            "SELECT delivery_batch_id,delivery_attempt_count FROM local_secret_outbox"
        ).fetchone() == (None, 0)
    finally:
        connection.close()
