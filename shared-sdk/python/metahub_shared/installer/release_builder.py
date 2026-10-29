"""Deterministic online catalog and offline media builder for PRD-124."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import os
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.private_gateway import (
    InstallerContractError,
    load_strict_json,
    verify_installer_artifacts,
    verify_installer_release,
)


@dataclass(frozen=True, slots=True)
class InstallerMediaBuild:
    online_catalog: Path
    offline_bundle: Path
    media_manifest: Path
    manifest_sha256: str
    online_sha256: str
    offline_sha256: str


def build_installer_media(
    *,
    release_root: Path,
    trust_store: Path,
    output_root: Path,
    current_schema: int = 116,
    agent_version: str = "1.0.0",
    control_version: str = "1.0.0",
) -> InstallerMediaBuild:
    """Verify one signed source tree, then derive both delivery channels from it."""

    source = release_root.resolve(strict=True)
    trust_path = trust_store.resolve(strict=True)
    output = output_root.resolve() if output_root.exists() else output_root.absolute()
    if output == source or source in output.parents:
        raise InstallerContractError("installer.output_invalid")
    envelope = load_strict_json(source / "manifest.json")
    verified = verify_installer_release(
        envelope,
        trusted_keys=_trusted_keys(trust_path),
        current_schema=current_schema,
        agent_version=agent_version,
        control_version=control_version,
    )
    artifacts = verify_installer_artifacts(verified, source)
    release = verified.release
    artifact_by_name = {item.filename: item for item in release.manifest.artifacts}
    ordered_artifacts = tuple(sorted(artifacts, key=lambda item: item.name))
    downloads = [
        {
            "artifact_id": artifact_by_name[path.name].artifact_id,
            "filename": path.name,
            "media_type": artifact_by_name[path.name].media_type,
            "sha256": artifact_by_name[path.name].sha256,
            "size_bytes": artifact_by_name[path.name].size_bytes,
            "executable": artifact_by_name[path.name].executable,
        }
        for path in ordered_artifacts
    ]
    online_document = {
        "schema_version": "metahub.installer-online/v1",
        "release": release.model_dump(mode="json"),
        "downloads": downloads,
    }
    online_bytes = canonical_json_bytes(online_document) + b"\n"
    prefix = f"metahub-private-gateway-{release.manifest.version}"
    offline_bytes = _offline_tar(
        prefix=prefix,
        manifest_bytes=canonical_json_bytes(release.model_dump(mode="json")) + b"\n",
        trust_store_bytes=canonical_json_bytes(load_strict_json(trust_path)) + b"\n",
        artifacts=ordered_artifacts,
        executable_names={item.filename for item in release.manifest.artifacts if item.executable},
    )
    online_name = f"{prefix}-online.json"
    offline_name = f"{prefix}-offline.tar"
    online_sha256 = hashlib.sha256(online_bytes).hexdigest()
    offline_sha256 = hashlib.sha256(offline_bytes).hexdigest()
    media_document = {
        "schema_version": "metahub.installer-media/v1",
        "release_id": release.manifest.release_id,
        "version": release.manifest.version,
        "manifest_sha256": release.manifest_sha256,
        "online": {
            "filename": online_name,
            "sha256": online_sha256,
            "size_bytes": len(online_bytes),
        },
        "offline": {
            "filename": offline_name,
            "sha256": offline_sha256,
            "size_bytes": len(offline_bytes),
        },
    }
    media_bytes = canonical_json_bytes(media_document) + b"\n"
    output.mkdir(parents=True, exist_ok=True)
    online_path = output / online_name
    offline_path = output / offline_name
    manifest_path = output / f"{prefix}-media.json"
    for path, content in (
        (online_path, online_bytes),
        (offline_path, offline_bytes),
        (manifest_path, media_bytes),
    ):
        _write_new(path, content)
    return InstallerMediaBuild(
        online_catalog=online_path,
        offline_bundle=offline_path,
        media_manifest=manifest_path,
        manifest_sha256=release.manifest_sha256,
        online_sha256=online_sha256,
        offline_sha256=offline_sha256,
    )


def _offline_tar(
    *,
    prefix: str,
    manifest_bytes: bytes,
    trust_store_bytes: bytes,
    artifacts: tuple[Path, ...],
    executable_names: set[str],
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        entries = (
            (f"{prefix}/manifest.json", manifest_bytes, 0o644),
            (f"{prefix}/trusted-keys.json", trust_store_bytes, 0o644),
            *(
                (
                    f"{prefix}/{path.name}",
                    path.read_bytes(),
                    0o755 if path.name in executable_names else 0o644,
                )
                for path in artifacts
            ),
        )
        for name, content, mode in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _trusted_keys(path: Path) -> dict[str, bytes]:
    value = load_strict_json(path, maximum_bytes=64 * 1024)
    try:
        if type(value) is not dict or set(value) != {"schema_version", "keys"}:
            raise ValueError
        if (
            value["schema_version"] != "metahub.installer-trust/v1"
            or type(value["keys"]) is not list
        ):
            raise ValueError
        result: dict[str, bytes] = {}
        for item in value["keys"]:
            if (
                type(item) is not dict
                or set(item) != {"key_id", "algorithm", "public_key", "purpose", "status"}
                or item["algorithm"] != "Ed25519"
                or item["purpose"] != "metahub.edge-installer.release/v1"
                or item["status"] not in {"active", "retired"}
                or type(item["key_id"]) is not str
                or type(item["public_key"]) is not str
                or "=" in item["public_key"]
            ):
                raise ValueError
            decoded = base64.b64decode(
                item["public_key"] + "=" * ((4 - len(item["public_key"]) % 4) % 4),
                altchars=b"-_",
                validate=True,
            )
            if len(decoded) != 32 or item["key_id"] in result:
                raise ValueError
            if item["status"] == "active":
                result[item["key_id"]] = decoded
        if not result:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError):
        raise InstallerContractError("installer.trust_store_invalid") from None


def _write_new(path: Path, content: bytes) -> None:
    if path.exists():
        raise InstallerContractError("installer.output_exists")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise InstallerContractError("installer.output_failed") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="metahub-installer-media")
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--trust-store", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = build_installer_media(
            release_root=arguments.release_root,
            trust_store=arguments.trust_store,
            output_root=arguments.output_root,
        )
        print(
            canonical_json_bytes(
                {
                    "status": "built",
                    "manifest_sha256": result.manifest_sha256,
                    "online_sha256": result.online_sha256,
                    "offline_sha256": result.offline_sha256,
                }
            ).decode("ascii")
        )
        return 0
    except InstallerContractError as error:
        print(canonical_json_bytes({"status": "error", "code": error.code}).decode("ascii"))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
