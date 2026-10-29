"""Strict, tenant-neutral installer release and preflight contracts for PRD-124."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from metahub_shared.config_distribution import canonical_json_bytes

INSTALLER_SCHEMA_VERSION = "metahub.installer/v1"
INSTALLER_SIGNATURE_PURPOSE = "metahub.edge-installer.release/v1"
PREFLIGHT_SCHEMA_VERSION = "metahub.preflight/v1"
MAX_INSTALLER_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024 * 1024
MAX_PREFLIGHT_MILLISECONDS = 120_000
PREFLIGHT_CHECK_CODES = (
    "artifact.integrity",
    "host.os",
    "host.architecture",
    "host.root",
    "host.cpu",
    "host.memory",
    "host.disk",
    "network.ports",
    "network.dns",
    "host.clock",
    "network.proxy",
    "network.ca",
    "host.filesystem",
    "runtime.container",
)

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^irl_[0-9a-f]{32}$")
_ARTIFACT_ID = re.compile(r"^ira_[0-9a-f]{32}$")
_PROFILE_ID = re.compile(r"^ipr_[0-9a-f]{32}$")
_REPORT_ID = re.compile(r"^pfr_[0-9a-f]{32}$")
_KEY_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,79}$")
_CODE = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_ARCHES = frozenset({"x86_64", "aarch64"})
_KINDS = frozenset({"cli", "compose", "container_bundle", "profile", "sbom"})
_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/spdx+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/x-tar",
        "application/yaml",
        "text/x-python",
    }
)
_FORBIDDEN_KEYS = re.compile(
    r"(?:authorization|cookie|credential|secret|token|password|passphrase|private[_-]?key|"
    r"access[_-]?key|api[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)


class InstallerContractError(ValueError):
    """Stable fail-closed installer validation error without untrusted details."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class InstallerArtifact(_StrictModel):
    artifact_id: str = Field(pattern=r"^ira_[0-9a-f]{32}$")
    kind: Literal["cli", "compose", "container_bundle", "profile", "sbom"]
    filename: str = Field(min_length=1, max_length=120)
    media_type: str = Field(min_length=3, max_length=96)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sbom_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=MAX_ARTIFACT_BYTES)
    executable: bool = False

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or len(path.parts) != 1
            or value in {".", ".."}
            or "\\" in value
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
        ):
            raise ValueError("installer.artifact_invalid")
        return value

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value: str) -> str:
        if value not in _MEDIA_TYPES:
            raise ValueError("installer.artifact_invalid")
        return value


class InstallProfile(_StrictModel):
    install_profile_id: str = Field(pattern=r"^ipr_[0-9a-f]{32}$")
    profile_code: Literal["standard", "compact"]
    display_name: str = Field(min_length=2, max_length=80)
    cpu_cores: int = Field(ge=2, le=256)
    memory_mib: int = Field(ge=2048, le=1_048_576)
    disk_mib: int = Field(ge=10_240, le=16_777_216)
    required_ports: list[int] = Field(min_length=1, max_length=16)
    dns_targets: list[str] = Field(min_length=1, max_length=8)
    runtime: Literal["docker"]
    runtime_min_version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    filesystem: Literal["local"]
    proxy_modes: list[Literal["direct", "http_connect"]] = Field(min_length=1, max_length=2)
    ca_mode: Literal["system", "system_or_custom"]

    @field_validator("required_ports")
    @classmethod
    def _ports(cls, value: list[int]) -> list[int]:
        if sorted(set(value)) != value or any(port < 1 or port > 65535 for port in value):
            raise ValueError("installer.profile_invalid")
        return value

    @field_validator("dns_targets")
    @classmethod
    def _dns_targets(cls, value: list[str]) -> list[str]:
        if sorted(set(value)) != value or any(
            len(item) > 253
            or re.fullmatch(
                r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                item,
            )
            is None
            for item in value
        ):
            raise ValueError("installer.profile_invalid")
        return value

    @field_validator("proxy_modes")
    @classmethod
    def _proxy_modes(cls, value: list[str]) -> list[str]:
        if sorted(set(value)) != value:
            raise ValueError("installer.profile_invalid")
        return value


class InstallerManifest(_StrictModel):
    schema_version: Literal["metahub.installer/v1"]
    release_id: str = Field(pattern=r"^irl_[0-9a-f]{32}$")
    version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    channel: Literal["stable", "extended"]
    signature_purpose: Literal["metahub.edge-installer.release/v1"]
    supported_os: list[Literal["linux"]]
    supported_arch: list[Literal["x86_64", "aarch64"]]
    schema_min: int = Field(ge=1, le=9999)
    schema_max: int = Field(ge=1, le=9999)
    agent_min_version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    agent_max_version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    control_min_version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    control_max_version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    n_minus_one: bool
    artifacts: list[InstallerArtifact] = Field(min_length=5, max_length=32)
    profiles: list[InstallProfile] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _invariants(self) -> InstallerManifest:
        artifact_ids = [item.artifact_id for item in self.artifacts]
        filenames = [item.filename for item in self.artifacts]
        profile_ids = [item.install_profile_id for item in self.profiles]
        kinds = {item.kind for item in self.artifacts}
        if (
            sorted(set(self.supported_arch)) != self.supported_arch
            or set(self.supported_arch) != _ARCHES
            or self.supported_os != ["linux"]
            or self.schema_min > self.schema_max
            or _version(self.agent_min_version) > _version(self.agent_max_version)
            or _version(self.control_min_version) > _version(self.control_max_version)
            or len(set(artifact_ids)) != len(artifact_ids)
            or len(set(filenames)) != len(filenames)
            or len(set(profile_ids)) != len(profile_ids)
            or not _KINDS <= kinds
            or sum(item.kind == "sbom" for item in self.artifacts) != 1
            or sum(item.kind == "profile" for item in self.artifacts) != 1
        ):
            raise ValueError("installer.manifest_invalid")
        sbom = next(item for item in self.artifacts if item.kind == "sbom")
        if any(item.sbom_sha256 != sbom.sha256 for item in self.artifacts):
            raise ValueError("installer.sbom_invalid")
        return self


class InstallerSignature(_StrictModel):
    algorithm: Literal["Ed25519"]
    key_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,79}$")
    public_key: str = Field(min_length=43, max_length=43, repr=False)
    value: str = Field(min_length=86, max_length=86, repr=False)


class InstallerRelease(_StrictModel):
    manifest: InstallerManifest
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: InstallerSignature


class PreflightCheck(_StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    outcome: Literal["passed", "warning", "failed"]
    required: bool
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    duration_ms: int = Field(ge=0, le=MAX_PREFLIGHT_MILLISECONDS)


class PreflightReport(_StrictModel):
    schema_version: Literal["metahub.preflight/v1"]
    preflight_report_id: str = Field(pattern=r"^pfr_[0-9a-f]{32}$")
    release_id: str = Field(pattern=r"^irl_[0-9a-f]{32}$")
    install_profile_id: str = Field(pattern=r"^ipr_[0-9a-f]{32}$")
    generation: int = Field(ge=1, le=2_147_483_647)
    outcome: Literal["passed", "failed"]
    environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: list[PreflightCheck] = Field(min_length=10, max_length=32)
    started_at: str = Field(min_length=20, max_length=32)
    completed_at: str = Field(min_length=20, max_length=32)
    duration_ms: int = Field(ge=0, le=MAX_PREFLIGHT_MILLISECONDS)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.astimezone(UTC).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z") != value:
                raise ValueError
        except ValueError:
            raise ValueError("installer.preflight_invalid") from None
        return value

    @model_validator(mode="after")
    def _outcome(self) -> PreflightReport:
        codes = [item.code for item in self.checks]
        failed = any(item.required and item.outcome == "failed" for item in self.checks)
        started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(self.completed_at.replace("Z", "+00:00"))
        elapsed_ms = int((completed - started).total_seconds() * 1000)
        if (
            codes != list(PREFLIGHT_CHECK_CODES)
            or self.outcome != ("failed" if failed else "passed")
            or elapsed_ms < 0
            or abs(elapsed_ms - self.duration_ms) > 2_000
        ):
            raise ValueError("installer.preflight_invalid")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedInstallerRelease:
    release: InstallerRelease
    canonical_manifest: bytes


def parse_installer_release(value: object) -> InstallerRelease:
    """Validate an exact release envelope and reject credential-like manifest fields."""

    try:
        _reject_sensitive_keys(value)
        release = InstallerRelease.model_validate(value)
        raw = canonical_json_bytes(release.manifest.model_dump(mode="json"))
        if len(raw) > MAX_INSTALLER_BYTES:
            raise InstallerContractError("installer.manifest_invalid")
        if hashlib.sha256(raw).hexdigest() != release.manifest_sha256:
            raise InstallerContractError("installer.manifest_digest_invalid")
        return release
    except InstallerContractError:
        raise
    except (RecursionError, TypeError, ValueError):
        raise InstallerContractError("installer.manifest_invalid") from None


def verify_installer_release(
    value: object,
    *,
    trusted_keys: Mapping[str, bytes],
    current_schema: int,
    agent_version: str,
    control_version: str,
) -> VerifiedInstallerRelease:
    """Verify trust, signature, and schema/Agent/Control N/N-1 compatibility."""

    release = parse_installer_release(value)
    manifest = release.manifest
    canonical = canonical_json_bytes(manifest.model_dump(mode="json"))
    signature = release.signature
    trusted = trusted_keys.get(signature.key_id)
    if type(trusted) is not bytes or len(trusted) != 32:
        raise InstallerContractError("installer.signing_key_untrusted")
    try:
        declared = _unb64(signature.public_key, 32)
        signed = _unb64(signature.value, 64)
        if declared != trusted:
            raise InstallerContractError("installer.signing_key_untrusted")
        Ed25519PublicKey.from_public_bytes(trusted).verify(
            signed,
            INSTALLER_SIGNATURE_PURPOSE.encode("ascii") + b"\0" + canonical,
        )
    except InstallerContractError:
        raise
    except (InvalidSignature, TypeError, ValueError):
        raise InstallerContractError("installer.signature_invalid") from None
    if not manifest.schema_min <= current_schema <= manifest.schema_max:
        raise InstallerContractError("installer.schema_incompatible")
    if not _compatible(agent_version, manifest.agent_min_version, manifest.agent_max_version):
        raise InstallerContractError("installer.agent_incompatible")
    if not _compatible(control_version, manifest.control_min_version, manifest.control_max_version):
        raise InstallerContractError("installer.control_incompatible")
    return VerifiedInstallerRelease(release=release, canonical_manifest=canonical)


def verify_installer_artifacts(
    verified: VerifiedInstallerRelease,
    artifact_root: Path,
) -> tuple[Path, ...]:
    """Verify every regular artifact under one fixed root before any install mutation."""

    try:
        if artifact_root.is_symlink():
            raise OSError
        root = artifact_root.resolve(strict=True)
        allowed = {item.filename for item in verified.release.manifest.artifacts}
        allowed.add("manifest.json")
        entries = tuple(root.iterdir())
        if any(
            item.name not in allowed or item.is_symlink() or not item.is_file()
            for item in entries
        ):
            raise InstallerContractError("installer.artifact_invalid")
        manifest_path = root / "manifest.json"
        stored_manifest = load_strict_json(manifest_path) if manifest_path.exists() else None
        if stored_manifest is not None and stored_manifest != verified.release.model_dump(
            mode="json"
        ):
            raise InstallerContractError("installer.manifest_digest_invalid")
    except OSError:
        raise InstallerContractError("installer.artifact_missing") from None
    checked: list[Path] = []
    structured: dict[str, object] = {}
    for artifact in verified.release.manifest.artifacts:
        candidate = artifact_root / artifact.filename
        try:
            if candidate.is_symlink():
                raise OSError
            path = candidate.resolve(strict=True)
            if path.parent != root or not path.is_file() or path.is_symlink():
                raise OSError
            stat = path.stat()
            if stat.st_size != artifact.size_bytes:
                raise InstallerContractError("installer.artifact_digest_invalid")
            digest = _file_sha256(path)
        except InstallerContractError:
            raise
        except OSError:
            raise InstallerContractError("installer.artifact_missing") from None
        if digest != artifact.sha256:
            raise InstallerContractError("installer.artifact_digest_invalid")
        checked.append(path)
        if artifact.kind in {"container_bundle", "profile", "sbom"}:
            document = load_strict_json(path, maximum_bytes=artifact.size_bytes)
            _reject_sensitive_keys(document)
            structured[artifact.kind] = document
    sbom = next(item for item in verified.release.manifest.artifacts if item.kind == "sbom")
    if any(item.sbom_sha256 != sbom.sha256 for item in verified.release.manifest.artifacts):
        raise InstallerContractError("installer.sbom_invalid")
    _verify_profile_document(
        structured.get("profile"), verified.release.manifest.profiles
    )
    _verify_container_document(structured.get("container_bundle"))
    _verify_spdx_document(
        structured.get("sbom"), version=verified.release.manifest.version
    )
    return tuple(checked)


def load_strict_json(path: Path, *, maximum_bytes: int = MAX_INSTALLER_BYTES) -> object:
    """Read duplicate-free canonicalizable JSON without exposing paths in failures."""

    try:
        raw = path.read_bytes()
        if not raw or len(raw) > maximum_bytes or raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError

        def pairs(items: Sequence[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, item in items:
                if key in result:
                    raise ValueError
                result[key] = item
            return result

        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        canonical_json_bytes(parsed)
        return parsed
    except (OSError, RecursionError, UnicodeError, ValueError):
        raise InstallerContractError("installer.manifest_invalid") from None


def installer_manifest_sha256(manifest: InstallerManifest | Mapping[str, object]) -> str:
    value = (
        manifest.model_dump(mode="json") if isinstance(manifest, InstallerManifest) else manifest
    )
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def installer_signature_payload(manifest: InstallerManifest | Mapping[str, object]) -> bytes:
    value = (
        manifest.model_dump(mode="json") if isinstance(manifest, InstallerManifest) else manifest
    )
    return INSTALLER_SIGNATURE_PURPOSE.encode("ascii") + b"\0" + canonical_json_bytes(value)


def _reject_sensitive_keys(value: object, depth: int = 0) -> None:
    if depth > 12:
        raise InstallerContractError("installer.manifest_invalid")
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str or _FORBIDDEN_KEYS.search(key):
                raise InstallerContractError("installer.sensitive_field_rejected")
            _reject_sensitive_keys(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_sensitive_keys(item, depth + 1)


def _verify_profile_document(value: object, profiles: list[InstallProfile]) -> None:
    expected = {
        "schema_version": "metahub.install-profiles/v1",
        "profiles": [item.model_dump(mode="json") for item in profiles],
    }
    if value != expected:
        raise InstallerContractError("installer.profile_invalid")


def _verify_container_document(value: object) -> None:
    if type(value) is not dict or set(value) != {"schema_version", "images"}:
        raise InstallerContractError("installer.artifact_invalid")
    images = value.get("images")
    if value.get("schema_version") != "metahub.container-bundle/v1" or not isinstance(
        images, list
    ):
        raise InstallerContractError("installer.artifact_invalid")
    components: list[str] = []
    for image in images:
        if type(image) is not dict or set(image) != {"component", "reference", "digest"}:
            raise InstallerContractError("installer.artifact_invalid")
        component = image.get("component")
        reference = image.get("reference")
        digest = image.get("digest")
        if (
            type(component) is not str
            or re.fullmatch(r"[a-z][a-z0-9-]{1,39}", component) is None
            or type(reference) is not str
            or re.fullmatch(r"[a-z0-9.-]+/[a-z0-9_./-]{1,180}", reference) is None
            or type(digest) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            raise InstallerContractError("installer.artifact_invalid")
        components.append(component)
    if len(images) < 1 or len(images) > 32 or sorted(set(components)) != components:
        raise InstallerContractError("installer.artifact_invalid")


def _verify_spdx_document(value: object, *, version: str) -> None:
    keys = {
        "SPDXID",
        "spdxVersion",
        "dataLicense",
        "name",
        "documentNamespace",
        "creationInfo",
        "packages",
    }
    if type(value) is not dict or set(value) != keys:
        raise InstallerContractError("installer.sbom_invalid")
    creation = value.get("creationInfo")
    packages = value.get("packages")
    if (
        value.get("SPDXID") != "SPDXRef-DOCUMENT"
        or value.get("spdxVersion") != "SPDX-2.3"
        or value.get("dataLicense") != "CC0-1.0"
        or type(value.get("name")) is not str
        or type(value.get("documentNamespace")) is not str
        or not str(value["documentNamespace"]).endswith(f"/{version}")
        or type(creation) is not dict
        or set(creation) != {"created", "creators"}
        or type(creation.get("created")) is not str
        or type(creation.get("creators")) is not list
        or not creation["creators"]
        or type(packages) is not list
        or not packages
        or len(packages) > 64
    ):
        raise InstallerContractError("installer.sbom_invalid")
    package_keys = {
        "SPDXID",
        "name",
        "versionInfo",
        "downloadLocation",
        "filesAnalyzed",
        "licenseConcluded",
        "licenseDeclared",
        "copyrightText",
    }
    names: list[str] = []
    for package in packages:
        if (
            type(package) is not dict
            or set(package) != package_keys
            or package.get("versionInfo") != version
            or type(package.get("name")) is not str
            or type(package.get("SPDXID")) is not str
            or package.get("filesAnalyzed") is not False
        ):
            raise InstallerContractError("installer.sbom_invalid")
        names.append(package["name"])
    if sorted(set(names)) != sorted(names) or set(names) != {
        "metahub-gateway",
        "metahub-local-control",
    }:
        raise InstallerContractError("installer.sbom_invalid")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _unb64(value: str, length: int) -> bytes:
    if type(value) is not str or "=" in value:
        raise ValueError
    decoded = base64.b64decode(
        value + "=" * ((4 - len(value) % 4) % 4), altchars=b"-_", validate=True
    )
    if len(decoded) != length:
        raise ValueError
    return decoded


def _version(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise ValueError("installer.version_invalid")
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def _compatible(value: str, minimum: str, maximum: str) -> bool:
    checked = _version(value)
    return _version(minimum) <= checked <= _version(maximum)


__all__ = (
    "INSTALLER_SCHEMA_VERSION",
    "INSTALLER_SIGNATURE_PURPOSE",
    "MAX_PREFLIGHT_MILLISECONDS",
    "PREFLIGHT_CHECK_CODES",
    "PREFLIGHT_SCHEMA_VERSION",
    "InstallProfile",
    "InstallerArtifact",
    "InstallerContractError",
    "InstallerManifest",
    "InstallerRelease",
    "InstallerSignature",
    "PreflightCheck",
    "PreflightReport",
    "VerifiedInstallerRelease",
    "installer_manifest_sha256",
    "installer_signature_payload",
    "load_strict_json",
    "parse_installer_release",
    "verify_installer_artifacts",
    "verify_installer_release",
)
