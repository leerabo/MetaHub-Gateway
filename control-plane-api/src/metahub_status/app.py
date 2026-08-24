"""Public status ASGI app with no Control database dependency."""

from __future__ import annotations

import base64
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.status_bundle import (
    StatusBundleError,
    VerifiedStatusBundle,
    parse_status_envelope,
    test_kms_status_public_key,
    verify_status_envelope,
)

_OBJECT_VERSION = re.compile(r"^status-bundle-[0-9]{20}\.json$")
_STATUS = frozenset(
    {"operational", "degraded_performance", "partial_outage", "major_outage", "maintenance"}
)


@dataclass(frozen=True, slots=True, repr=False)
class StatusPageConfig:
    bundle_root: Path
    trusted_keys: dict[str, bytes]

    @classmethod
    def from_environment(cls) -> StatusPageConfig:
        root = Path(os.getenv("METAHUB_STATUS_BUNDLE_ROOT", ".data/status-page")).resolve()
        test_secret = os.getenv("METAHUB_KMS_TEST_MASTER_KEY", "")
        keys: dict[str, bytes] = {}
        if test_secret:
            keys["test-v1"] = test_kms_status_public_key(test_secret)
        version = os.getenv("METAHUB_STATUS_SIGNING_KEY_VERSION", "")
        public_key = os.getenv("METAHUB_STATUS_SIGNING_PUBLIC_KEY", "")
        if version and public_key:
            try:
                keys[version] = base64.b64decode(
                    public_key + "=" * ((4 - len(public_key) % 4) % 4),
                    altchars=b"-_",
                    validate=True,
                )
            except Exception:
                keys.clear()
        return cls(bundle_root=root, trusted_keys=keys)


class StatusBundleReader:
    def __init__(self, config: StatusPageConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._cached: VerifiedStatusBundle | None = None

    def read(self, now: datetime | None = None) -> VerifiedStatusBundle:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self._lock:
            try:
                value = self._read_current(current)
                if self._cached is None or value.generation >= self._cached.generation:
                    self._cached = value
                return self._cached
            except (OSError, ValueError, KeyError, StatusBundleError):
                if self._cached is not None and self._cached.expires_at > current:
                    return self._cached
                raise StatusBundleError("status.bundle_invalid") from None

    def _read_current(self, now: datetime) -> VerifiedStatusBundle:
        raw_head = (self._config.bundle_root / "head.json").read_bytes()
        if not raw_head or len(raw_head) > 4096:
            raise ValueError
        head = json.loads(raw_head.decode("ascii"))
        if (
            type(head) is not dict
            or set(head) != {"generation", "object_version", "wire_schema_version"}
            or head["wire_schema_version"] != 1
            or type(head["generation"]) is not int
            or head["generation"] < 1
            or type(head["object_version"]) is not str
            or _OBJECT_VERSION.fullmatch(head["object_version"]) is None
            or canonical_json_bytes(head) != raw_head
        ):
            raise ValueError
        envelope = parse_status_envelope(
            (self._config.bundle_root / head["object_version"]).read_bytes()
        )
        verified = verify_status_envelope(
            envelope, trusted_keys=self._config.trusted_keys, now=now
        )
        if verified.generation != head["generation"]:
            raise ValueError
        _validate_payload(verified.payload)
        return verified


def create_status_app(config: StatusPageConfig | None = None) -> FastAPI:
    selected = config or StatusPageConfig.from_environment()
    reader = StatusBundleReader(selected)
    service = FastAPI(
        title="MetaHub Public Status",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    service.state.status_reader = reader

    @service.get("/public/status")
    async def public_status(request: Request) -> JSONResponse:
        try:
            bundle = request.app.state.status_reader.read()
        except StatusBundleError:
            return JSONResponse(
                {
                    "code": "status.unavailable",
                    "message": "Current service status is temporarily unavailable.",
                },
                status_code=503,
                headers={
                    "Cache-Control": "no-store",
                    "Retry-After": "30",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        body = {
            "wire_schema_version": 1,
            "generation": bundle.generation,
            "updated_at": _stamp(bundle.published_at),
            "expires_at": _stamp(bundle.expires_at),
            **bundle.payload,
        }
        return JSONResponse(
            body,
            headers={
                "Cache-Control": "public, max-age=30, stale-if-error=300",
                "X-Content-Type-Options": "nosniff",
                "X-Status-Generation": str(bundle.generation),
            },
        )

    @service.get("/health")
    async def health(request: Request) -> JSONResponse:
        try:
            bundle = request.app.state.status_reader.read()
        except StatusBundleError:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ok", "generation": bundle.generation})

    return service


def _validate_payload(value: object) -> None:
    if type(value) is not dict or set(value) != {
        "overall_status",
        "components",
        "incidents",
        "maintenance_windows",
    }:
        raise ValueError
    if value["overall_status"] not in _STATUS:
        raise ValueError
    components = value["components"]
    incidents = value["incidents"]
    maintenance = value["maintenance_windows"]
    if (
        type(components) is not list
        or not 1 <= len(components) <= 100
        or type(incidents) is not list
        or len(incidents) > 500
        or type(maintenance) is not list
        or len(maintenance) > 500
    ):
        raise ValueError
    for item in components:
        if type(item) is not dict or set(item) != {"code", "name", "group", "status", "revision"}:
            raise ValueError
        if item["status"] not in _STATUS or type(item["revision"]) is not int:
            raise ValueError
    for item in incidents:
        required = {
            "reference",
            "title",
            "severity",
            "status",
            "acknowledged_at",
            "updated_at",
            "updates",
        }
        if type(item) is not dict or not required <= set(item) <= required | {"postmortem"}:
            raise ValueError
        if (
            item["severity"] not in {"minor", "major", "critical"}
            or type(item["updates"]) is not list
        ):
            raise ValueError
    for item in maintenance:
        if type(item) is not dict or set(item) != {
            "reference",
            "title",
            "message",
            "status",
            "affected_components",
            "affected_regions",
            "starts_at",
            "ends_at",
        }:
            raise ValueError


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


app = create_status_app()

__all__ = ["StatusBundleReader", "StatusPageConfig", "app", "create_status_app"]
