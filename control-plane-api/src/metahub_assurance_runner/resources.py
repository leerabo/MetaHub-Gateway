"""Fixed local-resource observations in the disposable SQLite guest only.

No request-controlled SQL or storage paths, and no resource rows, keys or bodies
leave this module. These observations are not cleanup or execution attestations.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.files.contracts import FileContractError
from metahub_shared.files.crypto import (
    FILE_CIPHERTEXT_PREFIX,
    FileBlobOwner,
    decrypt_file_blob,
)
from metahub_shared.files.storage import EncryptedLocalBlobStore, file_object_key
from metahub_shared.observability import TelemetryBuffer, TelemetrySnapshot


class ResourceUnavailable(ValueError):
    pass


class ResourceContractViolation(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResourceObservation:
    resource_code: str
    identity_sha256: str
    state_sha256: str
    observed_at: datetime
    checks: tuple[tuple[str, bool], ...]


@dataclass(frozen=True, slots=True, repr=False)
class TelemetryCheckpoint:
    watermark: int
    snapshot: TelemetrySnapshot


def _digest(value) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _identity(value, prefix):
    if not isinstance(value, str) or re.fullmatch(prefix + r"_[0-9a-f]{32}", value) is None:
        raise ResourceContractViolation("resource.identity_invalid")


def _integrity_rejected(operation) -> bool:
    try:
        operation()
    except FileContractError as error:
        return error.code == "file.integrity_failed"
    return False


@dataclass(frozen=True, slots=True, repr=False)
class LocalResourceInspector:
    database: Path
    storage_root: Path
    file_secret: str
    telemetry_runtime: TelemetryBuffer | None = None
    telemetry_secret: str | None = None

    def __post_init__(self):
        for path in (self.database, self.storage_root):
            if not path.is_absolute() or any(
                part.is_symlink() or part.is_junction() for part in (path, *path.parents)
            ):
                raise ResourceUnavailable("resource.path_boundary")
        if (
            not self.database.is_file()
            or self.storage_root.parent != self.database.parent
            or self.storage_root == self.database
            or not isinstance(self.file_secret, str)
            or len(self.file_secret) < 48
        ):
            raise ResourceUnavailable("resource.profile_invalid")
        if (self.telemetry_runtime is None) != (self.telemetry_secret is None) or (
            self.telemetry_secret is not None
            and (not isinstance(self.telemetry_secret, str) or len(self.telemetry_secret) < 48)
        ):
            raise ResourceUnavailable("resource.telemetry_profile_invalid")

    def _read(self):
        connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=0.1)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
        except BaseException:
            connection.close()
            raise
        return connection

    def telemetry_checkpoint(self) -> TelemetryCheckpoint | None:
        if self.telemetry_runtime is None:
            raise ResourceUnavailable("resource.telemetry_missing")
        before = self.telemetry_runtime.snapshot()
        if before.dropped or before.backend_failures:
            raise ResourceUnavailable("resource.telemetry_degraded")
        if before.queued or before.accepted != before.persisted:
            return None
        try:
            with closing(self._read()) as connection:
                heads = connection.execute(
                    "SELECT source_watermark,dropped_fact_count FROM telemetry_ingest_head LIMIT 2"
                ).fetchall()
            if len(heads) != 1 or heads[0]["dropped_fact_count"]:
                raise ResourceUnavailable("resource.telemetry_head_invalid")
            if self.telemetry_runtime.snapshot() != before:
                return None
            return TelemetryCheckpoint(int(heads[0]["source_watermark"]), before)
        except (sqlite3.Error, OSError):
            raise ResourceUnavailable("resource.telemetry_unavailable") from None

    def telemetry_window(
        self,
        start: TelemetryCheckpoint,
        end: TelemetryCheckpoint,
        expected: tuple[tuple[str | None, str, int], ...],
    ) -> ResourceObservation:
        if self.telemetry_runtime is None or self.telemetry_secret is None:
            raise ResourceUnavailable("resource.telemetry_missing")
        counts = Counter()
        for tenant_id, status_class, count in expected:
            if tenant_id is not None:
                _identity(tenant_id, "ten")
            if status_class not in ("2xx", "4xx") or type(count) is not int or not 1 <= count <= 16:
                raise ResourceContractViolation("resource.telemetry_expectation_invalid")
            owner = "none" if tenant_id is None else hmac.new(
                self.telemetry_secret.encode(),
                f"prd109/telemetry-tenant/v1\0{tenant_id}".encode(),
                hashlib.sha256,
            ).hexdigest()
            counts[(owner, status_class)] += count
        total = sum(counts.values())
        if not 1 <= total <= 32 or not 0 <= start.watermark < end.watermark:
            raise ResourceContractViolation("resource.telemetry_window_invalid")
        try:
            with closing(self._read()) as connection:
                rows = [dict(row) for row in connection.execute(
                    """SELECT source_watermark,bucket_started_at,service_code,operation_code,
                              region_code,status_class,duration_bucket,tenant_pseudonym,
                              good_count,total_count,source_sha256
                       FROM sli_time_buckets WHERE source_watermark>? AND source_watermark<=?
                       ORDER BY source_watermark,sli_time_bucket_id LIMIT 257""",
                    (start.watermark, end.watermark),
                )]
            actual = Counter()
            for row in rows:
                actual[(row["tenant_pseudonym"], row["status_class"])] += row["total_count"]
            checks = (
                ("telemetry.nonempty_bounded", 0 < len(rows) <= 256),
                ("telemetry.exact_owner_counts", actual == counts),
                ("telemetry.buffer_accounted", (
                    end.snapshot.accepted - start.snapshot.accepted == total
                    and end.snapshot.persisted - start.snapshot.persisted == total
                    and end.snapshot.accepted == end.snapshot.persisted
                    and start.snapshot.accepted == start.snapshot.persisted
                    and not end.snapshot.queued and not start.snapshot.queued
                    and not end.snapshot.dropped and not end.snapshot.backend_failures
                )),
                ("telemetry.fixed_dimensions", all(
                    row["service_code"] == "control" and row["operation_code"] == "other"
                    and row["region_code"] == "global" and row["status_class"] in ("2xx", "4xx")
                    and row["duration_bucket"] in (
                        "le_10ms", "le_50ms", "le_100ms", "le_250ms", "le_500ms",
                        "le_1000ms", "le_2500ms", "le_5000ms", "gt_5000ms",
                    )
                    and (row["tenant_pseudonym"] == "none" or re.fullmatch(
                        r"[0-9a-f]{64}", row["tenant_pseudonym"]
                    ) is not None)
                    for row in rows
                )),
                ("telemetry.persisted_integrity", all(
                    type(row["total_count"]) is int and 0 < row["total_count"] <= total
                    and row["good_count"] == (
                        row["total_count"] if row["status_class"] == "2xx" else 0
                    )
                    and row["source_sha256"] == _digest({
                        key: value for key, value in row.items() if key != "source_sha256"
                    })
                    for row in rows
                )),
            )
            return ResourceObservation(
                "metric_bucket", _digest([start.watermark, end.watermark]), _digest(rows),
                datetime.now(UTC), checks,
            )
        except (sqlite3.Error, OSError):
            raise ResourceUnavailable("resource.telemetry_unavailable") from None

    def projection(self, workspace_id: str, tenant_id: str, public: dict) -> ResourceObservation:
        _identity(workspace_id, "wsp")
        _identity(tenant_id, "ten")
        try:
            with closing(self._read()) as connection:
                heads = connection.execute(
                    """SELECT * FROM workspace_navigation_projection_heads
                       WHERE workspace_id=? OR tenant_id=? LIMIT 3""",
                    (workspace_id, tenant_id),
                ).fetchall()
                if len(heads) != 1:
                    raise ResourceContractViolation("resource.projection_missing_or_ambiguous")
                head = dict(heads[0])
                projection_id = head["workspace_navigation_projection_id"]
                versions = connection.execute(
                    """SELECT * FROM workspace_navigation_projection_versions
                       WHERE workspace_navigation_projection_id=? LIMIT 2""",
                    (projection_id,),
                ).fetchall()
                if len(versions) != 1:
                    raise ResourceContractViolation("resource.projection_version_missing")
                version = dict(versions[0])
                items = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT * FROM workspace_navigation_projection_items
                       WHERE workspace_navigation_projection_id=?
                       ORDER BY source_navigation_id LIMIT 257""",
                        (projection_id,),
                    )
                ]
                sources = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT * FROM workspace_navigation_projection_sources
                       WHERE workspace_navigation_projection_id=? ORDER BY source_code LIMIT 17""",
                        (projection_id,),
                    )
                ]
            public_routes = {
                item["route_code"] for group in public["groups"] for item in group["items"]
            }
            checks = (
                ("projection.nonempty", 0 < len(items) <= 256 and 0 < len(sources) <= 16),
                (
                    "projection.owner_bound",
                    all(
                        row["workspace_id"] == workspace_id and row["tenant_id"] == tenant_id
                        for row in (head, version, *items, *sources)
                    ),
                ),
                (
                    "projection.version_bound",
                    (
                        head["generation"]
                        == version["generation"]
                        == public["projection_generation"]
                        and version["navigation_contract_snapshot_id"]
                        == public["contract_snapshot_id"]
                        and version["contract_generation"] == public["contract_generation"]
                        and version["status"] == public["projection_status"] == "ready"
                        and all(
                            row["projection_generation"] == head["generation"]
                            for row in (*items, *sources)
                        )
                    ),
                ),
                (
                    "projection.public_items_persisted",
                    bool(public_routes)
                    and public_routes.issubset({item["route_code"] for item in items}),
                ),
            )
            return ResourceObservation(
                "cached_projection",
                _digest([workspace_id, tenant_id, projection_id]),
                _digest([head, version, items, sources]),
                datetime.now(UTC),
                checks,
            )
        except (sqlite3.Error, OSError):
            raise ResourceUnavailable("resource.projection_unavailable") from None

    def file(
        self,
        tenant_id: str,
        file_id: str,
        foreign_tenant_id: str,
        expected: bytes,
    ) -> ResourceObservation:
        for value, prefix in ((tenant_id, "ten"), (foreign_tenant_id, "ten"), (file_id, "file")):
            _identity(value, prefix)
        if tenant_id == foreign_tenant_id or not 1 <= len(expected) <= 1024:
            raise ResourceContractViolation("resource.file_fixture_invalid")
        try:
            with closing(self._read()) as connection:
                rows = connection.execute(
                    """SELECT f.tenant_id,f.file_id,f.status,f.original_object_key,
                              f.content_fingerprint,e.file_extract_id,e.extractor_version,
                              e.status AS extract_status,e.text_fingerprint,e.extracted_object_key
                       FROM uploaded_files f JOIN file_extracts e
                         ON e.tenant_id=f.tenant_id AND e.file_id=f.file_id
                       WHERE f.file_id=? LIMIT 3""",
                    (file_id,),
                ).fetchall()
            if len(rows) != 1:
                raise ResourceContractViolation("resource.file_missing_or_ambiguous")
            row = dict(rows[0])
            if row["tenant_id"] != tenant_id:
                raise ResourceContractViolation("resource.file_owner_invalid")
            store = EncryptedLocalBlobStore(self.storage_root)
            checks = [("file.persisted_ready", row["status"] == row["extract_status"] == "ready")]
            ciphertexts = []
            for kind, key_column, fingerprint_column in (
                ("original", "original_object_key", "content_fingerprint"),
                ("extract", "extracted_object_key", "text_fingerprint"),
            ):
                key = file_object_key(file_id, kind)
                if row[key_column] != key:
                    raise ResourceContractViolation("resource.file_key_mismatch")
                target = self.storage_root.joinpath(*key.split("/"))
                if target.stat().st_size > 4096:
                    raise ResourceContractViolation("resource.file_size_invalid")
                ciphertext = store.read(key)
                ciphertexts.append(hashlib.sha256(ciphertext).hexdigest())
                owner = FileBlobOwner(
                    tenant_id=tenant_id,
                    file_id=file_id,
                    blob_kind=kind,
                    content_fingerprint=row[fingerprint_column],
                    file_extract_id=row["file_extract_id"] if kind == "extract" else None,
                    extractor_version=row["extractor_version"] if kind == "extract" else None,
                )
                checks.extend(
                    (
                        (
                            f"{kind}.encrypted",
                            ciphertext.startswith(FILE_CIPHERTEXT_PREFIX)
                            and expected not in ciphertext,
                        ),
                        (
                            f"{kind}.authenticated_content",
                            decrypt_file_blob(
                                ciphertext,
                                secret=self.file_secret,
                                owner=owner,
                            )
                            == expected,
                        ),
                    )
                )
                for label, forged in (
                    ("tenant_swap", replace(owner, tenant_id=foreign_tenant_id)),
                    ("file_swap", replace(owner, file_id="file_" + "0" * 32)),
                    (
                        "kind_swap",
                        replace(
                            owner,
                            blob_kind="filename",
                            file_extract_id=None,
                            extractor_version=None,
                        ),
                    ),
                ):
                    checks.append(
                        (
                            f"{kind}.{label}_denied",
                            _integrity_rejected(
                                lambda forged=forged, ciphertext=ciphertext: decrypt_file_blob(
                                    ciphertext,
                                    secret=self.file_secret,
                                    owner=forged,
                                )
                            ),
                        )
                    )
                for label, invalid in (
                    ("traversal", "../" + key),
                    ("separator", key.replace("/", "\\")),
                    ("absolute", "/" + key),
                ):
                    checks.append(
                        (
                            f"{kind}.{label}_read_denied",
                            _integrity_rejected(
                                lambda invalid=invalid: store.read(invalid),
                            ),
                        )
                    )
                    checks.append(
                        (
                            f"{kind}.{label}_stage_denied",
                            _integrity_rejected(
                                lambda invalid=invalid, ciphertext=ciphertext: store.stage(
                                    ciphertext,
                                    object_key=invalid,
                                ),
                            ),
                        )
                    )
                staged = store.stage(ciphertext, object_key=key)
                try:
                    checks.append(
                        (
                            f"{kind}.overwrite_denied",
                            _integrity_rejected(
                                lambda staged=staged: store.publish(staged),
                            ),
                        )
                    )
                finally:
                    store.discard(staged)
                checks.append((f"{kind}.staging_removed", not staged.path.exists()))
                checks.append((f"{kind}.ciphertext_unchanged", store.read(key) == ciphertext))
            return ResourceObservation(
                "object_artifact",
                _digest([tenant_id, file_id, row["file_extract_id"]]),
                _digest([row, ciphertexts]),
                datetime.now(UTC),
                tuple(checks),
            )
        except FileContractError as error:
            if error.code == "file.integrity_failed":
                raise ResourceContractViolation("resource.file_integrity_failed") from None
            raise ResourceUnavailable("resource.file_unavailable") from None
        except (sqlite3.Error, OSError):
            raise ResourceUnavailable("resource.file_unavailable") from None
