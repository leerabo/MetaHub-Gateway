"""Bounded Redis, S3-compatible object store, and KMS health probes."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Literal

ProbeStatus = Literal["healthy", "degraded", "unavailable", "not_configured"]
_PROBE_ADMISSION = threading.BoundedSemaphore(4)


@dataclass(frozen=True, slots=True)
class InfrastructureProbe:
    status: ProbeStatus
    version_summary: str | None
    latency_ms: int | None
    verified: bool
    error_code: str | None = None


def probe_redis(redis_url: str | None, *, timeout_seconds: int) -> InfrastructureProbe:
    if not redis_url:
        return _not_configured()
    started = time.perf_counter()
    if not _PROBE_ADMISSION.acquire(blocking=False):
        return _degraded(started, "infrastructure.probe_busy")
    client = None
    try:
        import redis

        client = redis.Redis.from_url(
            redis_url,
            socket_connect_timeout=timeout_seconds,
            socket_timeout=timeout_seconds,
            decode_responses=True,
        )
        if client.ping() is not True:
            return _failed(started, "infrastructure.redis_probe_failed")
        info = client.info(section="server")
        version = str(info.get("redis_version") or "redis")[:80]
        return _healthy(started, version)
    except ImportError:
        return _failed(started, "infrastructure.redis_driver_missing")
    except Exception:
        return _failed(started, "infrastructure.redis_unavailable")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        _PROBE_ADMISSION.release()


def probe_object_store(
    *,
    endpoint_url: str | None,
    region: str | None,
    bucket: str | None,
    access_key_id: str | None,
    secret_access_key: str | None,
    force_path_style: bool,
    timeout_seconds: int,
) -> InfrastructureProbe:
    configured = (endpoint_url, region, bucket, access_key_id, secret_access_key)
    if not any(configured):
        return _not_configured()
    if not all(configured):
        return InfrastructureProbe(
            status="degraded",
            version_summary=None,
            latency_ms=None,
            verified=False,
            error_code="infrastructure.object_store_config_incomplete",
        )
    started = time.perf_counter()
    if not _PROBE_ADMISSION.acquire(blocking=False):
        return _degraded(started, "infrastructure.probe_busy")
    client = None
    try:
        import boto3
        from botocore.config import Config

        addressing = "path" if force_path_style else "virtual"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries={"max_attempts": 1, "mode": "standard"},
                s3={"addressing_style": addressing},
            ),
        )
        client.head_bucket(Bucket=bucket)
        return _healthy(started, "s3-compatible")
    except ImportError:
        return _failed(started, "infrastructure.object_store_driver_missing")
    except Exception:
        return _failed(started, "infrastructure.object_store_unavailable")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        _PROBE_ADMISSION.release()


def probe_kms(
    *,
    provider: Literal["disabled", "aws", "test"],
    key_id: str | None,
    test_master_key: str | None,
    region: str | None,
    timeout_seconds: int,
) -> InfrastructureProbe:
    if provider == "disabled":
        return _not_configured()
    if provider == "test":
        if not test_master_key or len(test_master_key) < 32:
            return InfrastructureProbe(
                status="degraded",
                version_summary=None,
                latency_ms=None,
                verified=False,
                error_code="infrastructure.kms_config_incomplete",
            )
        return InfrastructureProbe(
            status="healthy",
            version_summary="local-test-v1",
            latency_ms=0,
            verified=True,
        )
    if not key_id:
        return InfrastructureProbe(
            status="degraded",
            version_summary=None,
            latency_ms=None,
            verified=False,
            error_code="infrastructure.kms_config_incomplete",
        )
    started = time.perf_counter()
    if not _PROBE_ADMISSION.acquire(blocking=False):
        return _degraded(started, "infrastructure.probe_busy")
    client = None
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "kms",
            region_name=region,
            config=Config(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
        metadata = client.describe_key(KeyId=key_id).get("KeyMetadata") or {}
        if metadata.get("Enabled") is not True or metadata.get("KeyState") != "Enabled":
            return _failed(started, "infrastructure.kms_key_unavailable")
        manager = str(metadata.get("KeyManager") or "kms").lower()
        return _healthy(started, f"kms-{manager}"[:80])
    except ImportError:
        return _failed(started, "infrastructure.kms_driver_missing")
    except Exception:
        return _failed(started, "infrastructure.kms_unavailable")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        _PROBE_ADMISSION.release()


def _healthy(started: float, version: str) -> InfrastructureProbe:
    return InfrastructureProbe(
        status="healthy",
        version_summary=version,
        latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        verified=True,
    )


def _failed(started: float, code: str) -> InfrastructureProbe:
    return InfrastructureProbe(
        status="unavailable",
        version_summary=None,
        latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        verified=False,
        error_code=code,
    )


def _degraded(started: float, code: str) -> InfrastructureProbe:
    return InfrastructureProbe(
        status="degraded",
        version_summary=None,
        latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        verified=False,
        error_code=code,
    )


def _not_configured() -> InfrastructureProbe:
    return InfrastructureProbe(
        status="not_configured",
        version_summary=None,
        latency_ms=None,
        verified=False,
    )
