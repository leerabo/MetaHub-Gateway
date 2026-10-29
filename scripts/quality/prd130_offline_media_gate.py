"""Run the PRD-130 near-10GiB signed offline-media verification gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared-sdk" / "python"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from metahub_shared.private_gateway import (  # noqa: E402
    MAX_OFFLINE_BUNDLE_BYTES,
    MAX_OFFLINE_ENTRY_BYTES,
    OfflineBundleKeyEnvelope,
    OfflineDomainVersion,
    build_offline_bundle_archive,
    verify_offline_bundle_archive,
)
from metahub_shared.private_gateway.offline_bundles import (  # noqa: E402
    MAX_OFFLINE_MANIFEST_BYTES,
)

DEFAULT_CIPHERTEXT_BYTES = MAX_OFFLINE_BUNDLE_BYTES - MAX_OFFLINE_MANIFEST_BYTES
SLO_VERIFY_SECONDS = 600.0
WORKSPACE_ID = "wsp_" + "1" * 32
TENANT_ID = "11111111-1111-4111-8111-111111111111"
DEPLOYMENT_ID = "edp_" + "2" * 32
KEY_ID = "prd130-media-gate-v1"


def _entry_plaintext_sizes(ciphertext_bytes: int) -> tuple[int, ...]:
    if not 17 <= ciphertext_bytes <= DEFAULT_CIPHERTEXT_BYTES:
        raise ValueError("ciphertext_bytes_out_of_range")
    maximum_ciphertext_size = MAX_OFFLINE_ENTRY_BYTES + 16
    entry_count = (
        ciphertext_bytes + maximum_ciphertext_size - 1
    ) // maximum_ciphertext_size
    sizes: list[int] = []
    remaining = ciphertext_bytes
    for index in range(entry_count):
        remaining_entries = entry_count - index - 1
        entry_ciphertext_size = min(
            maximum_ciphertext_size,
            remaining - (remaining_entries * 17),
        )
        if entry_ciphertext_size < 17:
            raise ValueError("ciphertext_bytes_cannot_form_entries")
        sizes.append(entry_ciphertext_size - 16)
        remaining -= entry_ciphertext_size
    if remaining != 0:
        raise ValueError("ciphertext_bytes_cannot_form_entries")
    return tuple(sizes)


def _write_sparse_source(path: Path, size: int) -> None:
    with path.open("xb") as handle:
        handle.truncate(size)


def run_gate(*, ciphertext_bytes: int) -> dict[str, Any]:
    sizes = _entry_plaintext_sizes(ciphertext_bytes)
    temp_parent = ROOT / ".tmp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    work_root = Path(tempfile.mkdtemp(prefix="prd130-media-gate-", dir=temp_parent))
    started = time.perf_counter()
    try:
        sources: dict[int, Path] = {}
        for size in set(sizes):
            source = work_root / f"source-{size}.bin"
            _write_sparse_source(source, size)
            sources[size] = source

        private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        content_key = bytes(range(32, 64))
        envelope = OfflineBundleKeyEnvelope(
            encryption_key_version=1,
            kms_key_id_sha256=hashlib.sha256(b"prd130-media-gate-kms-key").hexdigest(),
            wrapped_key="cHJkMTMwLW1lZGlhLWdhdGUtd3JhcHBlZC1rZXk",
        )
        version = OfflineDomainVersion(
            domain="status",
            base_generation=0,
            generation=1,
            value_sha256=hashlib.sha256(b"prd130-media-gate-status-v1").hexdigest(),
        )
        output = work_root / "offline-media.mhb"
        now = datetime.now(UTC)
        build_started = time.perf_counter()
        signed = build_offline_bundle_archive(
            output,
            direction="cloud_to_edge",
            workspace_id=WORKSPACE_ID,
            tenant_id=TENANT_ID,
            deployment_id=DEPLOYMENT_ID,
            version_vector=(version,),
            entries=tuple(
                ("status", "application/octet-stream", sources[size]) for size in sizes
            ),
            content_key=content_key,
            key_envelope=envelope,
            private_key=private_key,
            key_id=KEY_ID,
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
        build_seconds = time.perf_counter() - build_started
        archive_bytes = output.stat().st_size

        verify_started = time.perf_counter()
        verified = verify_offline_bundle_archive(
            output,
            work_root / "staging",
            trusted_keys={KEY_ID: private_key.public_key().public_bytes_raw()},
            unwrap_key=lambda _envelope: content_key,
            expected_workspace_id=WORKSPACE_ID,
            expected_tenant_id=TENANT_ID,
            expected_deployment_id=DEPLOYMENT_ID,
            expected_direction="cloud_to_edge",
            now=datetime.now(UTC),
        )
        verify_seconds = time.perf_counter() - verify_started
        verified_sizes = tuple(
            verified.entry_paths[item.entry_id].stat().st_size for item in signed.manifest.entries
        )
        payload_bytes = sum(sizes)
        verified_payload_bytes = sum(verified_sizes)
        archive_within_limit = archive_bytes <= MAX_OFFLINE_BUNDLE_BYTES
        complete = (
            len(verified_sizes) == len(sizes)
            and sorted(verified_sizes) == sorted(sizes)
            and verified_payload_bytes == payload_bytes
        )
        slo_passed = verify_seconds < SLO_VERIFY_SECONDS
        return {
            "schema": "metahub.prd130.offline-media-gate.v1",
            "status": "passed" if archive_within_limit and complete and slo_passed else "failed",
            "platform": platform.system().lower(),
            "target_ciphertext_bytes": ciphertext_bytes,
            "payload_bytes": payload_bytes,
            "archive_bytes": archive_bytes,
            "entry_count": len(sizes),
            "verified_entry_count": len(verified_sizes),
            "verified_payload_bytes": verified_payload_bytes,
            "archive_within_10gib": archive_within_limit,
            "verification_complete": complete,
            "build_seconds": round(build_seconds, 3),
            "verify_seconds": round(verify_seconds, 3),
            "total_seconds": round(time.perf_counter() - started, 3),
            "slo_verify_seconds": SLO_VERIFY_SECONDS,
            "slo_passed": slo_passed,
        }
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ciphertext-bytes", type=int, default=DEFAULT_CIPHERTEXT_BYTES)
    args = parser.parse_args()
    try:
        report = run_gate(ciphertext_bytes=args.ciphertext_bytes)
    except Exception as exc:
        report = {
            "schema": "metahub.prd130.offline-media-gate.v1",
            "status": "failed",
            "failure_type": type(exc).__name__,
            "failure_code": getattr(exc, "code", None),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "archive_bytes": report.get("archive_bytes", 0),
                "verify_seconds": report.get("verify_seconds", 0),
                "slo_passed": report.get("slo_passed", False),
            },
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
