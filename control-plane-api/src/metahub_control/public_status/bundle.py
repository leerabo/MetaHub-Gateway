"""Signed, versioned, atomically published public status projections."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.status_bundle import (
    StatusBundleError,
    build_status_envelope,
    parse_status_envelope,
    signed_status_document,
    test_kms_status_public_key,
    verify_status_envelope,
)

from metahub_control.config_distribution.crypto import sign_config_manifest


def publish_status_bundle(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    operator_id: str | None,
    now: datetime,
) -> int:
    generation = int(
        connection.execute(
            "SELECT COALESCE(MAX(generation),0)+1 FROM status_publication_bundles"
        ).fetchone()[0]
    )
    published_at = _stamp(now)
    expires_at = _stamp(now + timedelta(seconds=config.status_bundle_ttl_seconds))
    payload = _public_projection(connection)
    signed = signed_status_document(
        generation=generation,
        published_at=published_at,
        expires_at=expires_at,
        payload=payload,
    )
    signature = sign_config_manifest(config, signed)
    envelope = build_status_envelope(
        signed,
        algorithm=signature.algorithm,
        key_version=signature.key_version,
        signature=signature.value,
    )
    trusted = {signature.key_version: _verification_key(config, signature.key_version)}
    try:
        verify_status_envelope(envelope, trusted_keys=trusted, now=now)
    except StatusBundleError:
        raise ApiError("status.publication_unavailable") from None
    object_version = f"status-bundle-{generation:020d}.json"
    _atomic_publish(config.status_bundle_root, object_version, envelope, generation)
    connection.execute(
        "UPDATE status_publication_bundles SET status='superseded' WHERE status='published'"
    )
    connection.execute(
        """
        INSERT INTO status_publication_bundles(
          status_publication_bundle_id,generation,canonical_sha256,signature_algorithm,
          signature_value,signing_key_version,object_version,status,publisher_kind,
          created_by_operator_id,published_at,expires_at)
        VALUES(?,?,?,?,?,?,?,'published',?,?,?,?)
        """,
        (
            "spb_" + uuid4().hex,
            generation,
            signed["payload_sha256"],
            signature.algorithm,
            signature.value,
            signature.key_version,
            object_version,
            "system" if operator_id is None else "operator",
            operator_id,
            published_at,
            expires_at,
        ),
    )
    return generation


def current_status_bundle_is_fresh(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    now: datetime,
) -> bool:
    """Check the immutable head without trusting it enough to serve public traffic."""

    row = connection.execute(
        """SELECT generation,canonical_sha256,signing_key_version,object_version,expires_at
           FROM status_publication_bundles WHERE status='published'"""
    ).fetchone()
    if row is None:
        return False
    try:
        expires_at = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
        refresh_margin = timedelta(seconds=max(60, config.status_bundle_ttl_seconds // 3))
        if expires_at.astimezone(UTC) <= now.astimezone(UTC) + refresh_margin:
            return False
        root = Path(config.status_bundle_root).resolve()
        raw_head = (root / "head.json").read_bytes()
        head = json.loads(raw_head.decode("ascii"))
        if (
            type(head) is not dict
            or set(head) != {"generation", "object_version", "wire_schema_version"}
            or canonical_json_bytes(head) != raw_head
            or head["wire_schema_version"] != 1
            or head["generation"] != int(row["generation"])
            or head["object_version"] != row["object_version"]
        ):
            return False
        envelope = parse_status_envelope((root / str(row["object_version"])).read_bytes())
        return (
            envelope["generation"] == int(row["generation"])
            and envelope["payload_sha256"] == row["canonical_sha256"]
            and envelope["signing_key_version"] == row["signing_key_version"]
            and envelope["expires_at"] == row["expires_at"]
        )
    except (OSError, ValueError, KeyError, StatusBundleError):
        return False


def _public_projection(connection: sqlite3.Connection) -> dict[str, object]:
    components = [
        {
            "code": row["public_code"],
            "name": row["public_name"],
            "group": row["component_group"],
            "status": row["current_state"],
            "revision": int(row["revision"]),
        }
        for row in connection.execute(
            """SELECT public_code,public_name,component_group,current_state,revision
               FROM status_components ORDER BY display_order"""
        ).fetchall()
    ]
    incidents: list[dict[str, object]] = []
    for row in connection.execute(
        """
        SELECT * FROM incidents WHERE visibility='public'
          AND status IN('published','monitoring','resolved','postmortem_published')
        ORDER BY acknowledged_at DESC,incident_id DESC
        """
    ).fetchall():
        updates = [
            {
                "status": update["status"],
                "message": update["public_message"],
                "affected_components": _json_array(update["affected_components_json"]),
                "affected_regions": _json_array(update["affected_regions_json"]),
                "published_at": update["published_at"],
            }
            for update in connection.execute(
                """SELECT status,public_message,affected_components_json,
                          affected_regions_json,published_at
                   FROM incident_updates WHERE incident_id=?
                   ORDER BY published_at,incident_update_id""",
                (row["incident_id"],),
            ).fetchall()
        ]
        postmortem = connection.execute(
            "SELECT * FROM postmortems WHERE incident_id=? AND status='published'",
            (row["incident_id"],),
        ).fetchone()
        item: dict[str, object] = {
            "reference": row["public_reference"],
            "title": row["title"],
            "severity": row["severity"],
            "status": row["status"],
            "acknowledged_at": row["acknowledged_at"],
            "updated_at": row["updated_at"],
            "updates": updates,
        }
        if postmortem is not None:
            item["postmortem"] = {
                "summary": postmortem["public_summary"],
                "timeline": _json_array(postmortem["timeline_json"]),
                "cause_category": postmortem["cause_category"],
                "corrective_actions": _json_array(postmortem["corrective_actions_json"]),
                "published_at": postmortem["published_at"],
            }
        incidents.append(item)
    maintenance = [
        {
            "reference": row["public_reference"],
            "title": row["title"],
            "message": row["public_message"],
            "status": row["status"],
            "affected_components": _json_array(row["affected_components_json"]),
            "affected_regions": _json_array(row["affected_regions_json"]),
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
        }
        for row in connection.execute(
            """SELECT * FROM maintenance_windows WHERE visibility='public'
               AND status IN('planned','in_progress')
               ORDER BY starts_at,maintenance_window_id"""
        ).fetchall()
    ]
    states = {str(item["status"]) for item in components}
    if any(item["severity"] == "critical" and item["status"] != "resolved" for item in incidents):
        overall = "major_outage"
    elif "partial_outage" in states or any(
        item["severity"] == "major" and item["status"] != "resolved" for item in incidents
    ):
        overall = "partial_outage"
    elif "degraded_performance" in states or any(
        item["status"] not in {"resolved", "postmortem_published"} for item in incidents
    ):
        overall = "degraded_performance"
    elif "maintenance" in states or maintenance:
        overall = "maintenance"
    else:
        overall = "operational"
    return {
        "overall_status": overall,
        "components": components,
        "incidents": incidents,
        "maintenance_windows": maintenance,
    }


def _atomic_publish(
    configured_root: str,
    object_version: str,
    envelope: dict[str, object],
    generation: int,
) -> None:
    root = Path(configured_root).resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        document = canonical_json_bytes(envelope)
        version_path = root / object_version
        _replace_file(version_path, document)
        head = canonical_json_bytes(
            {"generation": generation, "object_version": object_version, "wire_schema_version": 1}
        )
        _replace_file(root / "head.json", head)
    except OSError:
        raise ApiError("status.publication_unavailable") from None


def _replace_file(path: Path, value: bytes) -> None:
    handle = tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _verification_key(config: AppConfig, key_version: str) -> bytes:
    if config.kms_provider == "test" and config.kms_test_master_key:
        return test_kms_status_public_key(config.kms_test_master_key)
    if config.kms_provider != "aws" or not config.config_distribution_signing_key_id:
        raise ApiError("status.publication_unavailable")
    client = None
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "kms",
            region_name=config.object_store_region,
            config=Config(
                connect_timeout=config.infrastructure_probe_timeout_seconds,
                read_timeout=config.infrastructure_probe_timeout_seconds,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
        result = client.get_public_key(KeyId=config.config_distribution_signing_key_id)
        value = bytes(result["PublicKey"])
        returned_id = str(result.get("KeyId") or config.config_distribution_signing_key_id)
        expected = "kmsv_" + hashlib.sha256(returned_id.encode()).hexdigest()[:24]
        if not value or expected != key_version:
            raise ValueError
        return value
    except Exception:
        raise ApiError("status.publication_unavailable") from None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def _json_array(value: object) -> list[str]:
    parsed = json.loads(str(value))
    if type(parsed) is not list or any(type(item) is not str for item in parsed):
        raise ApiError("status.publication_unavailable")
    return parsed


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = ["current_status_bundle_is_fresh", "publish_status_bundle"]
