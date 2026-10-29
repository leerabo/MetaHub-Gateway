"""mTLS upload of customer-approved encrypted support bundles."""

from __future__ import annotations

import hashlib
import re
import struct
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from metahub_shared.private_gateway import (
    EncryptedSupportBundle,
    SupportBundleUploadMetadata,
    SupportBundleUploadReceipt,
    canonical_edge_json_bytes,
    strict_json_object,
)

_EDGE_SESSION = re.compile(r"^egs_[A-Za-z0-9_-]{43}$")
_MAX_METADATA_BYTES = 65_536
_MAX_RECEIPT_BYTES = 8_192


class SupportBundleUploadError(RuntimeError):
    """Fixed upload failure that does not retain response or path details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CloudSupportBundleUploader:
    """Stream one immutable ciphertext object to Control over an mTLS client."""

    def __init__(
        self,
        client: httpx.Client,
        control_base_url: str,
        *,
        edge_session: Callable[[], str],
    ) -> None:
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
            or not callable(edge_session)
        ):
            raise SupportBundleUploadError("local_support_bundle.endpoint_invalid")
        self._client = client
        self._url = f"{base}/edge/support-bundles"
        self._edge_session = edge_session

    def __call__(
        self,
        bundle: EncryptedSupportBundle,
        *,
        tenant_id: str,
        workspace_id: str,
        approval_account_id: str,
        approval_proof_sha256: str,
        approved_at: str,
    ) -> str:
        del tenant_id, workspace_id
        session = self._edge_session()
        if type(session) is not str or _EDGE_SESSION.fullmatch(session) is None:
            raise SupportBundleUploadError("local_support_bundle.authentication_failed")
        _verify_object(bundle.object_path, bundle.object_sha256, bundle.object_size_bytes)
        metadata = SupportBundleUploadMetadata(
            schema_version="metahub.support-bundle-upload/v1",
            manifest=bundle.manifest,
            manifest_sha256=_manifest_sha256(bundle),
            object_sha256=bundle.object_sha256,
            object_size_bytes=bundle.object_size_bytes,
            approved_by_account_id=approval_account_id,
            approval_proof_sha256=approval_proof_sha256,
            approved_at=approved_at,
        )
        encoded = canonical_edge_json_bytes(
            metadata.model_dump(mode="json"), maximum=_MAX_METADATA_BYTES
        )
        content_length = 4 + len(encoded) + bundle.object_size_bytes
        try:
            with self._client.stream(
                "POST",
                self._url,
                content=_body(encoded, bundle.object_path),
                headers={
                    "Accept": "application/json",
                    "Cache-Control": "no-store",
                    "Content-Length": str(content_length),
                    "Content-Type": "application/vnd.metahub.edge-support-bundle",
                    "X-Edge-Deployment-ID": bundle.manifest.deployment_id,
                    "X-Edge-Session": session,
                },
                follow_redirects=False,
            ) as response:
                response_body = _read_bounded(response)
                if (
                    response.status_code not in {200, 201}
                    or response.is_redirect
                    or str(response.url) != self._url
                    or response.headers.get("content-type", "").partition(";")[0].lower()
                    != "application/json"
                    or "no-store" not in response.headers.get("cache-control", "").lower()
                ):
                    raise SupportBundleUploadError("local_support_bundle.upload_failed")
            receipt = SupportBundleUploadReceipt.model_validate(
                strict_json_object(response_body, canonical=False, maximum=_MAX_RECEIPT_BYTES)
            )
            if (
                receipt.bundle_id != bundle.manifest.bundle_id
                or receipt.cloud_reference != bundle.manifest.bundle_id
                or receipt.object_sha256 != bundle.object_sha256
            ):
                raise SupportBundleUploadError("local_support_bundle.receipt_invalid")
            return receipt.cloud_reference
        except SupportBundleUploadError:
            raise
        except (httpx.HTTPError, OSError, RuntimeError):
            raise SupportBundleUploadError("local_support_bundle.upload_failed") from None
        except (TypeError, ValueError):
            raise SupportBundleUploadError("local_support_bundle.receipt_invalid") from None


def _manifest_sha256(bundle: EncryptedSupportBundle) -> str:
    from metahub_shared.private_gateway import support_bundle_manifest_sha256

    return support_bundle_manifest_sha256(bundle.manifest)


def _verify_object(path: Path, expected_sha256: str, expected_size: int) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        raise SupportBundleUploadError("local_support_bundle.integrity_failed") from None
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise SupportBundleUploadError("local_support_bundle.integrity_failed")


def _body(metadata: bytes, path: Path) -> Iterator[bytes]:
    yield struct.pack(">I", len(metadata))
    yield metadata
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                yield chunk
    except OSError:
        raise SupportBundleUploadError("local_support_bundle.upload_failed") from None


def _read_bounded(response: httpx.Response) -> bytes:
    lengths = response.headers.get_list("content-length")
    if len(lengths) > 1:
        raise SupportBundleUploadError("local_support_bundle.receipt_invalid")
    if lengths and (
        not lengths[0].isascii()
        or not lengths[0].isdecimal()
        or int(lengths[0]) > _MAX_RECEIPT_BYTES
    ):
        raise SupportBundleUploadError("local_support_bundle.receipt_invalid")
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > _MAX_RECEIPT_BYTES:
            raise SupportBundleUploadError("local_support_bundle.receipt_invalid")
        body.extend(chunk)
    return bytes(body)


__all__ = ("CloudSupportBundleUploader", "SupportBundleUploadError")
