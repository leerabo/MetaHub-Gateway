"""Strict Agent-to-Control delivery for cloud-safe local secret facts."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx
from metahub_shared.private_gateway import (
    EdgeSecretSyncBatch,
    EdgeSecretSyncReceipt,
    canonical_edge_json_bytes,
)

from .store import LocalSecretStore

_EDGE_SESSION = re.compile(r"^egs_[A-Za-z0-9_-]{43}$")
_MAX_RECEIPT_BYTES = 8_192


class LocalSecretSyncError(RuntimeError):
    """Fixed synchronization failure that never retains transport details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LocalSecretSyncUploader:
    """Upload one retry-stable safe-fact batch over an externally configured mTLS client."""

    def __init__(self, client: httpx.Client, control_base_url: str) -> None:
        base = control_base_url.rstrip("/")
        parsed = urlsplit(base)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "http" and not loopback)
        ):
            raise LocalSecretSyncError("local_secret.endpoint_invalid")
        self._client = client
        self._url = f"{base}/internal/edge/secret-references/sync"

    def sync(
        self,
        store: LocalSecretStore,
        *,
        edge_session: str,
        limit: int = 64,
    ) -> int:
        """Deliver at most one batch and return its exactly acknowledged event count."""

        if _EDGE_SESSION.fullmatch(edge_session) is None:
            raise LocalSecretSyncError("local_secret.authentication_failed")
        batch = store.next_sync_batch(limit=limit)
        if batch is None:
            return 0
        receipt = self._send(batch, edge_session=edge_session)
        if (
            receipt.batch_id != batch.batch_id
            or receipt.deployment_id != batch.deployment_id
            or receipt.accepted_event_count + receipt.duplicate_event_count != len(batch.events)
        ):
            raise LocalSecretSyncError("local_secret.receipt_invalid")
        try:
            return store.acknowledge_sync_receipt(receipt)
        except Exception as exc:
            if getattr(exc, "code", None) == "local_secret.receipt_invalid":
                raise LocalSecretSyncError("local_secret.receipt_invalid") from None
            raise

    def _send(self, batch: EdgeSecretSyncBatch, *, edge_session: str) -> EdgeSecretSyncReceipt:
        body = canonical_edge_json_bytes(batch.model_dump(mode="json"))
        try:
            with self._client.stream(
                "POST",
                self._url,
                content=body,
                headers={
                    "Accept": "application/json",
                    "Cache-Control": "no-store",
                    "Content-Type": "application/json",
                    "X-Edge-Session": edge_session,
                },
                follow_redirects=False,
            ) as response:
                response_body = _read_bounded(response)
                media_type = response.headers.get("content-type", "").partition(";")[0]
                if (
                    response.status_code != 200
                    or response.is_redirect
                    or str(response.url) != self._url
                    or media_type.lower() != "application/json"
                    or "no-store" not in response.headers.get("cache-control", "").lower()
                ):
                    raise LocalSecretSyncError("local_secret.receipt_invalid")
            payload = _strict_json_object(response_body)
            return EdgeSecretSyncReceipt.model_validate(payload)
        except LocalSecretSyncError:
            raise
        except (httpx.HTTPError, OSError, RuntimeError):
            raise LocalSecretSyncError("local_secret.transport_unavailable") from None
        except (TypeError, ValueError):
            raise LocalSecretSyncError("local_secret.receipt_invalid") from None


def _read_bounded(response: httpx.Response) -> bytes:
    content_lengths = response.headers.get_list("content-length")
    if len(content_lengths) > 1:
        raise LocalSecretSyncError("local_secret.receipt_invalid")
    if content_lengths:
        raw_length = content_lengths[0]
        if (
            not raw_length.isascii()
            or not raw_length.isdecimal()
            or int(raw_length) > _MAX_RECEIPT_BYTES
        ):
            raise LocalSecretSyncError("local_secret.receipt_invalid")
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > _MAX_RECEIPT_BYTES:
            raise LocalSecretSyncError("local_secret.receipt_invalid")
        body.extend(chunk)
    return bytes(body)


class _DuplicateJsonKey(ValueError):
    pass


def _strict_json_object(body: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise LocalSecretSyncError("local_secret.receipt_invalid") from None
    if type(value) is not dict:
        raise LocalSecretSyncError("local_secret.receipt_invalid")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError


__all__ = ("LocalSecretSyncError", "LocalSecretSyncUploader")
