"""Retry-stable delivery of cloud-safe backup and restore-drill summaries."""

from __future__ import annotations

import hmac
import json
import re
from urllib.parse import urlsplit

import httpx

from .backups import LocalBackupError, LocalBackupStore, LocalCloudReport

_EDGE_SESSION = re.compile(r"^egs_[A-Za-z0-9_-]{43}$")
_MAX_RECEIPT_BYTES = 65_536


class LocalBackupReportUploadError(RuntimeError):
    """Fixed delivery failure that does not retain response or credential details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LocalBackupReportUploader:
    """Deliver one durable report with an externally authenticated mTLS client."""

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
            raise LocalBackupReportUploadError("local_backup.endpoint_invalid")
        self._client = client
        self._base_url = base

    def upload_next(self, store: LocalBackupStore, *, edge_session: str) -> int:
        if _EDGE_SESSION.fullmatch(edge_session) is None:
            raise LocalBackupReportUploadError("local_backup.authentication_failed")
        report = store.next_cloud_report()
        if report is None:
            return 0
        self._send(report, edge_session=edge_session)
        try:
            return int(
                store.acknowledge_cloud_report(report.report_id, report.payload_sha256)
            )
        except LocalBackupError as exc:
            raise LocalBackupReportUploadError(exc.code) from None

    def _send(self, report: LocalCloudReport, *, edge_session: str) -> None:
        if report.report_kind == "backup":
            path = (
                f"/edge/deployments/{report.deployment_id}/backups/reports"
            )
            accepted_statuses = {200, 201}
        else:
            path = (
                f"/edge/deployments/{report.deployment_id}/restore-drills/"
                f"{report.aggregate_id}/reports"
            )
            accepted_statuses = {200}
        url = self._base_url + path
        try:
            with self._client.stream(
                "POST",
                url,
                content=report.payload_json,
                headers={
                    "Accept": "application/json",
                    "Cache-Control": "no-store",
                    "Content-Type": "application/json",
                    "X-Edge-Session": edge_session,
                },
                follow_redirects=False,
            ) as response:
                body = _read_bounded(response)
                media_type = response.headers.get("content-type", "").partition(";")[0]
                if (
                    response.status_code not in accepted_statuses
                    or response.is_redirect
                    or str(response.url) != url
                    or media_type.lower() != "application/json"
                    or "no-store" not in response.headers.get(
                        "cache-control", ""
                    ).lower()
                ):
                    raise LocalBackupReportUploadError(
                        "local_backup.receipt_invalid"
                    )
            payload = _strict_json_object(body)
            _validate_receipt(report, payload)
        except LocalBackupReportUploadError:
            raise
        except (httpx.HTTPError, OSError, RuntimeError):
            raise LocalBackupReportUploadError(
                "local_backup.transport_unavailable"
            ) from None
        except (TypeError, ValueError):
            raise LocalBackupReportUploadError("local_backup.receipt_invalid") from None


def _validate_receipt(report: LocalCloudReport, payload: dict[str, object]) -> None:
    if set(payload) != {
        "deployment_id",
        "idempotent_replay",
        "request_id",
        "trace_id",
        "backup" if report.report_kind == "backup" else "drill",
    } or type(payload["idempotent_replay"]) is not bool:
        raise LocalBackupReportUploadError("local_backup.receipt_invalid")
    if not _same(payload["deployment_id"], report.deployment_id):
        raise LocalBackupReportUploadError("local_backup.receipt_invalid")
    key = "backup" if report.report_kind == "backup" else "drill"
    resource = payload[key]
    if type(resource) is not dict:
        raise LocalBackupReportUploadError("local_backup.receipt_invalid")
    identifier_key = "backup_id" if report.report_kind == "backup" else "drill_id"
    if not _same(resource.get(identifier_key), report.aggregate_id):
        raise LocalBackupReportUploadError("local_backup.receipt_invalid")
    if report.report_kind == "drill" and resource.get("status") not in {
        "succeeded",
        "failed",
    }:
        raise LocalBackupReportUploadError("local_backup.receipt_invalid")
    for key in ("request_id", "trace_id"):
        if type(payload[key]) is not str or not 1 <= len(payload[key]) <= 160:
            raise LocalBackupReportUploadError("local_backup.receipt_invalid")


def _same(value: object, expected: str) -> bool:
    return type(value) is str and hmac.compare_digest(value, expected)


def _read_bounded(response: httpx.Response) -> bytes:
    content_lengths = response.headers.get_list("content-length")
    if len(content_lengths) > 1:
        raise LocalBackupReportUploadError("local_backup.receipt_invalid")
    if content_lengths:
        raw = content_lengths[0]
        if not raw.isascii() or not raw.isdecimal() or int(raw) > _MAX_RECEIPT_BYTES:
            raise LocalBackupReportUploadError("local_backup.receipt_invalid")
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > _MAX_RECEIPT_BYTES:
            raise LocalBackupReportUploadError("local_backup.receipt_invalid")
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
        raise LocalBackupReportUploadError("local_backup.receipt_invalid") from None
    if type(value) is not dict:
        raise LocalBackupReportUploadError("local_backup.receipt_invalid")
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


__all__ = ("LocalBackupReportUploadError", "LocalBackupReportUploader")
