"""Crash-safe local execution for signed private gateway releases."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.private_gateway import (
    InstallerContractError,
    InstallProfile,
    PreflightCheck,
    PreflightReport,
    VerifiedInstallerRelease,
    build_registration_challenge_proof,
    parse_edge_csr,
    validate_edge_challenge,
    validate_edge_deployment_id,
    validate_edge_registration_token,
    verify_installer_artifacts,
)

_INSTALLATION_ID = re.compile(r"^ins_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")
_MAX_AUDIT_BYTES = 16 * 1024 * 1024
_MAX_HTTP_BYTES = 256 * 1024
_JOURNAL_SCHEMA = "metahub.install-journal/v1"
_AUDIT_SCHEMA = "metahub.install-audit/v1"
_INSTALL_STEPS = (
    "staging_prepared",
    "artifacts_copied",
    "runtime_validated",
    "runtime_started",
    "health_verified",
    "release_committed",
)


class InstallerRuntimeError(RuntimeError):
    """Fixed error code boundary that never includes paths or command output."""

    def __init__(self, code: str) -> None:
        checked = code if _SAFE_CODE.fullmatch(code) else "installer.failed"
        super().__init__(checked)
        self.code = checked


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InstallJournal(_StrictModel):
    schema_version: Literal["metahub.install-journal/v1"]
    installation_id: str = Field(pattern=r"^ins_[0-9a-f]{32}$")
    release_id: str = Field(pattern=r"^irl_[0-9a-f]{32}$")
    install_profile_id: str = Field(pattern=r"^ipr_[0-9a-f]{32}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(ge=1, le=2_147_483_647)
    status: Literal[
        "installing",
        "installed",
        "connecting",
        "active",
        "failed",
        "uninstalling",
        "uninstalled",
    ]
    current_step: str | None = Field(default=None, max_length=80)
    completed_steps: list[str] = Field(default_factory=list, max_length=32)
    receipts: dict[str, str] = Field(default_factory=dict)
    compensation_status: Literal["not_required", "pending", "completed", "failed"]
    deployment_id: str | None = Field(default=None, pattern=r"^edp_[0-9a-f]{32}$")
    retain_config: bool = True
    retain_audit: bool = True
    retain_secrets: bool = True
    audit_head_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: str = Field(min_length=20, max_length=32)
    updated_at: str = Field(min_length=20, max_length=32)

    @model_validator(mode="after")
    def _steps(self) -> InstallJournal:
        if (
            len(set(self.completed_steps)) != len(self.completed_steps)
            or any(
                step not in {*_INSTALL_STEPS, "device_registered", "initial_snapshot"}
                for step in self.completed_steps
            )
            or set(self.receipts) != set(self.completed_steps)
            or any(_SHA256.fullmatch(item) is None for item in self.receipts.values())
            or (self.status == "active" and self.deployment_id is None)
        ):
            raise ValueError("installer.journal_invalid")
        return self


class RegistrationMaterial(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    registration_token: str = Field(min_length=47, max_length=47, repr=False)
    challenge: str = Field(min_length=47, max_length=47, repr=False)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""


class CommandRunner(Protocol):
    def run(self, arguments: Sequence[str], *, timeout: float) -> CommandResult: ...


class RegistrationClient(Protocol):
    def register(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...

    def confirm_initial_snapshot(
        self,
        *,
        deployment_id: str,
        certificate_path: Path,
        private_key_path: Path,
    ) -> bool: ...


class SubprocessCommandRunner:
    def run(self, arguments: Sequence[str], *, timeout: float) -> CommandResult:
        try:
            completed = subprocess.run(
                list(arguments),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
            )
            return CommandResult(completed.returncode, completed.stdout[:256])
        except (OSError, subprocess.SubprocessError):
            return CommandResult(127)


class SystemProbe:
    """Read-only host probes; results are reduced to fixed outcomes before persistence."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        environ: Mapping[str, str] | None = None,
        custom_ca: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.runner = runner or SubprocessCommandRunner()
        self.environ = environ if environ is not None else os.environ
        self.custom_ca = custom_ca
        self.now = now or (lambda: datetime.now(UTC))

    def os_name(self) -> str:
        return platform.system().lower()

    def architecture(self) -> str:
        value = platform.machine().lower()
        return {"amd64": "x86_64", "arm64": "aarch64"}.get(value, value)

    def cpu_cores(self) -> int:
        return int(os.cpu_count() or 0)

    def memory_mib(self) -> int:
        try:
            pages = int(os.sysconf("SC_PHYS_PAGES"))
            size = int(os.sysconf("SC_PAGE_SIZE"))
            return pages * size // (1024 * 1024)
        except (AttributeError, OSError, TypeError, ValueError):
            return 0

    def disk_mib(self, target: Path) -> int:
        current = target
        while not current.exists() and current != current.parent:
            current = current.parent
        try:
            return shutil.disk_usage(current).free // (1024 * 1024)
        except OSError:
            return 0

    def ports_available(self, ports: Sequence[int]) -> bool:
        sockets: list[socket.socket] = []
        try:
            for port in ports:
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                listener.bind(("127.0.0.1", port))
                sockets.append(listener)
            return True
        except OSError:
            return False
        finally:
            for listener in sockets:
                listener.close()

    def dns_available(self, targets: Sequence[str]) -> bool:
        try:
            return all(
                socket.getaddrinfo(target, 443, type=socket.SOCK_STREAM) for target in targets
            )
        except OSError:
            return False

    def clock_synchronized(self) -> bool:
        result = self.runner.run(
            ("timedatectl", "show", "--property=NTPSynchronized", "--value"),
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "yes"

    def proxy_valid(self, profile: InstallProfile) -> bool:
        values = [
            self.environ.get(name, "")
            for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
            if self.environ.get(name)
        ]
        if not values:
            return "direct" in profile.proxy_modes
        if "http_connect" not in profile.proxy_modes:
            return False
        for value in values:
            try:
                parsed = urllib.parse.urlsplit(value)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.path not in {"", "/"}
                    or parsed.query
                    or parsed.fragment
                ):
                    return False
            except ValueError:
                return False
        return True

    def ca_valid(self) -> bool:
        try:
            if self.custom_ca is None:
                context = ssl.create_default_context()
            else:
                path = self.custom_ca.resolve(strict=True)
                if not path.is_file() or path.is_symlink():
                    return False
                context = ssl.create_default_context(cafile=str(path))
            return bool(context.get_ca_certs())
        except (OSError, ssl.SSLError):
            return False

    def filesystem_valid(self, target: Path) -> bool:
        current = target
        while not current.exists() and current != current.parent:
            current = current.parent
        try:
            info = current.stat()
            return (
                stat.S_ISDIR(info.st_mode)
                and not current.is_symlink()
                and os.access(current, os.W_OK)
            )
        except OSError:
            return False

    def runtime_version(self) -> str | None:
        result = self.runner.run(
            ("docker", "version", "--format", "{{.Server.Version}}"), timeout=10
        )
        value = result.stdout.strip()
        return value if result.returncode == 0 and _VERSION.fullmatch(value) else None

    def root_available(self) -> bool:
        return hasattr(os, "geteuid") and os.geteuid() == 0


class UrlRegistrationClient:
    """HTTPS client that keeps registration material out of argv, output, and URLs."""

    def __init__(self, control_url: str, *, timeout: float = 30.0) -> None:
        self.origin = _control_origin(control_url)
        self.timeout = timeout

    def register(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        return self._json_request("POST", "/edge/registrations", payload=payload)

    def confirm_initial_snapshot(
        self,
        *,
        deployment_id: str,
        certificate_path: Path,
        private_key_path: Path,
    ) -> bool:
        if not self.origin.startswith("https://"):
            return False
        context = ssl.create_default_context()
        try:
            context.load_cert_chain(str(certificate_path), str(private_key_path))
            session = self._json_request(
                "POST",
                "/edge/sessions",
                payload={
                    "deployment_id": deployment_id,
                    "handshake_nonce": "ecn_" + secrets.token_urlsafe(32),
                    "ttl_seconds": 45,
                },
                context=context,
            )
            token = session.get("session_token")
            if type(token) is not str or not 32 <= len(token) <= 4096:
                return False
            pulled = self._json_request(
                "GET",
                f"/edge/deployments/{deployment_id}/desired-state",
                headers={"X-Edge-Session": token},
                context=context,
            )
            return set(pulled) == {"deployment_id", "state", "trust", "request_id", "trace_id"}
        except InstallerRuntimeError:
            return False

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        context: ssl.SSLContext | None = None,
    ) -> Mapping[str, object]:
        body = canonical_json_bytes(payload) if payload is not None else None
        request_headers = {"Accept": "application/json", **(headers or {})}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.origin + path,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                raw = response.read(_MAX_HTTP_BYTES + 1)
                if len(raw) > _MAX_HTTP_BYTES or response.status not in {200, 201}:
                    raise ValueError
                value = json.loads(raw.decode("utf-8"))
                if type(value) is not dict:
                    raise ValueError
                return value
        except (OSError, UnicodeError, ValueError, urllib.error.URLError):
            raise InstallerRuntimeError("installer.control_unavailable") from None


def run_preflight(
    verified: VerifiedInstallerRelease,
    *,
    install_profile_id: str,
    artifact_root: Path,
    target_root: Path,
    generation: int = 1,
    probe: SystemProbe | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> PreflightReport:
    """Run bounded checks without creating or changing any persistent host state."""

    selected = _profile(verified, install_profile_id)
    host = probe or SystemProbe()
    started = host.now()
    timer = monotonic()
    checks: list[PreflightCheck] = []

    def check(
        code: str,
        required: bool,
        operation: Callable[[], bool],
        failure: str,
    ) -> None:
        check_start = monotonic()
        try:
            passed = operation()
        except Exception:
            passed = False
        duration = min(120_000, max(0, int((monotonic() - check_start) * 1000)))
        checks.append(
            PreflightCheck(
                code=code,
                outcome="passed" if passed else ("failed" if required else "warning"),
                required=required,
                reason_code="ok" if passed else failure,
                duration_ms=duration,
            )
        )

    artifacts_ok = True
    try:
        verify_installer_artifacts(verified, artifact_root)
    except InstallerContractError:
        artifacts_ok = False
    runtime = host.runtime_version()
    listen_ports = [port for port in selected.required_ports if port != 443]
    check("artifact.integrity", True, lambda: artifacts_ok, "artifact_invalid")
    check("host.os", True, lambda: host.os_name() == "linux", "linux_required")
    check(
        "host.architecture",
        True,
        lambda: host.architecture() in verified.release.manifest.supported_arch,
        "architecture_unsupported",
    )
    check("host.root", True, host.root_available, "root_required")
    check("host.cpu", True, lambda: host.cpu_cores() >= selected.cpu_cores, "cpu_insufficient")
    check(
        "host.memory",
        True,
        lambda: host.memory_mib() >= selected.memory_mib,
        "memory_insufficient",
    )
    check(
        "host.disk",
        True,
        lambda: host.disk_mib(target_root) >= selected.disk_mib,
        "disk_insufficient",
    )
    check("network.ports", True, lambda: host.ports_available(listen_ports), "port_unavailable")
    check(
        "network.dns",
        True,
        lambda: host.dns_available(selected.dns_targets),
        "dns_unavailable",
    )
    check("host.clock", True, host.clock_synchronized, "clock_unsynchronized")
    check("network.proxy", True, lambda: host.proxy_valid(selected), "proxy_invalid")
    check("network.ca", True, host.ca_valid, "ca_unavailable")
    check(
        "host.filesystem",
        True,
        lambda: host.filesystem_valid(target_root),
        "filesystem_unsupported",
    )
    check(
        "runtime.container",
        True,
        lambda: runtime is not None and _version(runtime) >= _version(selected.runtime_min_version),
        "runtime_incompatible",
    )
    completed = host.now()
    duration_ms = min(120_000, max(0, int((monotonic() - timer) * 1000)))
    environment = {
        "architecture": host.architecture(),
        "cpu_bucket": _bucket(host.cpu_cores(), (2, 4, 8, 16, 32)),
        "disk_bucket": _bucket(host.disk_mib(target_root), (10_240, 40_960, 102_400)),
        "memory_bucket": _bucket(host.memory_mib(), (2_048, 8_192, 16_384, 32_768)),
        "os": host.os_name(),
        "runtime_major": int(runtime.split(".", 1)[0]) if runtime else 0,
    }
    return PreflightReport(
        schema_version="metahub.preflight/v1",
        preflight_report_id="pfr_" + uuid4().hex,
        release_id=verified.release.manifest.release_id,
        install_profile_id=selected.install_profile_id,
        generation=generation,
        outcome=(
            "failed"
            if any(item.required and item.outcome == "failed" for item in checks)
            else "passed"
        ),
        environment_sha256=_sha(environment),
        checks=checks,
        started_at=_time(started),
        completed_at=_time(completed),
        duration_ms=duration_ms,
    )


def install_release(
    verified: VerifiedInstallerRelease,
    report: PreflightReport,
    *,
    artifact_root: Path,
    target_root: Path,
    runner: CommandRunner | None = None,
    health_probe: Callable[[], bool] | None = None,
    confirmation: str = "",
    require_root: bool = True,
) -> InstallJournal:
    """Install through staging and durable receipts, compensating every pre-commit failure."""

    root = _safe_root(target_root)
    manifest = verified.release.manifest
    _validate_report(verified, report)
    if report.outcome != "passed":
        raise InstallerRuntimeError("installer.preflight_failed")
    if require_root:
        _require_confirmation(
            confirmation,
            f"INSTALL {manifest.release_id} PROFILE {report.install_profile_id} "
            f"GENERATION {report.generation}",
        )
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise InstallerRuntimeError("installer.root_required")
    artifacts = verify_installer_artifacts(verified, artifact_root)
    installation_id = _installation_id(
        root, manifest.release_id, report.install_profile_id, report.generation
    )
    state_dir = root / "state" / "installations" / installation_id
    journal_path = state_dir / "journal.json"
    release_dir = root / "releases" / manifest.release_id
    staging = root / ".staging" / f"{installation_id}-{report.generation}"
    now = _time(datetime.now(UTC))
    if journal_path.exists():
        journal = _load_journal(journal_path)
        _same_install(journal, verified, report)
        if journal.status in {"installed", "connecting", "active"}:
            return journal
        if journal.status == "failed" and "release_committed" in journal.completed_steps:
            raise InstallerRuntimeError("installer.state_conflict")
        if journal.status == "failed":
            journal.status = "installing"
            journal.current_step = None
            journal.completed_steps = []
            journal.receipts = {}
            journal.compensation_status = "not_required"
            journal.updated_at = now
        elif journal.status != "installing":
            raise InstallerRuntimeError("installer.state_conflict")
    else:
        state_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        journal = InstallJournal(
            schema_version=_JOURNAL_SCHEMA,
            installation_id=installation_id,
            release_id=manifest.release_id,
            install_profile_id=report.install_profile_id,
            manifest_sha256=verified.release.manifest_sha256,
            generation=report.generation,
            status="installing",
            compensation_status="not_required",
            audit_head_sha256="0" * 64,
            started_at=now,
            updated_at=now,
        )
    _write_journal(journal_path, journal)
    _audit(root, journal, "edge.install_started", "started", "install_started")
    _write_journal(journal_path, journal)
    command_runner = runner or SubprocessCommandRunner()
    health = health_probe or _default_health_probe

    try:
        _install_step(
            root,
            journal_path,
            journal,
            "staging_prepared",
            {"generation": report.generation},
            lambda: _prepare_staging(staging),
        )
        _install_step(
            root,
            journal_path,
            journal,
            "artifacts_copied",
            {"manifest_sha256": journal.manifest_sha256},
            lambda: _copy_artifacts(
                artifacts,
                staging,
                {item.filename: item.sha256 for item in manifest.artifacts},
            ),
        )
        compose_root = release_dir if release_dir.exists() else staging
        compose = compose_root / _artifact_filename(verified, "compose")
        _install_step(
            root,
            journal_path,
            journal,
            "runtime_validated",
            {"compose_sha256": _file_sha(compose)},
            lambda: _require_command(
                command_runner,
                ("docker", "compose", "-f", str(compose), "config", "--quiet"),
                timeout=60,
                code="installer.runtime_validation_failed",
            ),
        )
        _install_step(
            root,
            journal_path,
            journal,
            "runtime_started",
            {"compose_sha256": _file_sha(compose)},
            lambda: _require_command(
                command_runner,
                ("docker", "compose", "-f", str(compose), "up", "-d", "--pull", "never"),
                timeout=600,
                code="installer.runtime_start_failed",
            ),
        )
        _install_step(
            root,
            journal_path,
            journal,
            "health_verified",
            {"probe": "gateway.ready/v1"},
            lambda: _require_health(health),
        )
        _install_step(
            root,
            journal_path,
            journal,
            "release_committed",
            {"manifest_sha256": journal.manifest_sha256},
            lambda: _commit_release(staging, release_dir, verified),
        )
        journal.status = "installed"
        journal.current_step = None
        journal.updated_at = _time(datetime.now(UTC))
        _audit(root, journal, "edge.install_completed", "succeeded", "software_installed")
        _write_journal(journal_path, journal)
        return journal
    except Exception as exc:
        failure = (
            exc
            if isinstance(exc, InstallerRuntimeError)
            else InstallerRuntimeError("installer.install_failed")
        )
        journal.status = "failed"
        journal.compensation_status = "pending"
        journal.updated_at = _time(datetime.now(UTC))
        _write_journal(journal_path, journal)
        try:
            compose = (
                release_dir / _artifact_filename(verified, "compose")
                if release_dir.exists()
                else staging / _artifact_filename(verified, "compose")
            )
            if "runtime_started" in journal.completed_steps and compose.exists():
                _require_command(
                    command_runner,
                    ("docker", "compose", "-f", str(compose), "down", "--remove-orphans"),
                    timeout=300,
                    code="installer.compensation_failed",
                )
            if release_dir.exists() and "release_committed" not in journal.completed_steps:
                _remove_tree(release_dir, root)
            _remove_tree(staging, root)
            journal.compensation_status = "completed"
        except Exception:
            journal.compensation_status = "failed"
        journal.current_step = None
        journal.updated_at = _time(datetime.now(UTC))
        _audit(root, journal, "edge.install_failed", "failed", failure.code)
        _write_journal(journal_path, journal)
        if isinstance(exc, InstallerRuntimeError):
            raise
        raise failure from None


def connect_installation(
    *,
    target_root: Path,
    installation_id: str,
    registration: RegistrationMaterial,
    client: RegistrationClient,
    agent_version: str,
    capability_codes: Sequence[str] = (
        "config_apply",
        "secret_reference",
        "telemetry",
        "upgrade",
    ),
    health_probe: Callable[[], bool] | None = None,
    confirmation: str = "",
    require_root: bool = True,
) -> InstallJournal:
    """Register one stable local key/CSR and confirm the PRD-121 initial pull."""

    root = _safe_root(target_root)
    _checked_installation_id(installation_id)
    if require_root:
        _require_confirmation(confirmation, f"CONNECT {installation_id}")
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise InstallerRuntimeError("installer.root_required")
    journal_path = root / "state" / "installations" / installation_id / "journal.json"
    journal = _load_journal(journal_path)
    if journal.status == "active":
        if journal.deployment_id != registration.deployment_id:
            raise InstallerRuntimeError("installer.registration_conflict")
        return journal
    recoverable_failure = (
        journal.status == "failed" and "release_committed" in journal.completed_steps
    )
    if journal.status not in {"installed", "connecting"} and not recoverable_failure:
        raise InstallerRuntimeError("installer.state_conflict")
    validate_edge_deployment_id(registration.deployment_id)
    validate_edge_registration_token(registration.registration_token)
    validate_edge_challenge(registration.challenge)
    if _VERSION.fullmatch(agent_version) is None:
        raise InstallerRuntimeError("installer.agent_incompatible")
    if list(capability_codes) != sorted(set(capability_codes)):
        raise InstallerRuntimeError("installer.request_invalid")
    journal.status = "connecting"
    journal.updated_at = _time(datetime.now(UTC))
    _write_journal(journal_path, journal)
    secret_dir = root / "secrets" / installation_id
    secret_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    key_path = secret_dir / "device-key.pem"
    certificate_path = secret_dir / "device-certificate.pem"
    ca_path = secret_dir / "edge-ca.pem"
    private_key = _load_or_create_key(key_path)
    csr = _csr(private_key)
    parsed = parse_edge_csr(csr)
    proof = build_registration_challenge_proof(
        registration.registration_token,
        deployment_id=registration.deployment_id,
        challenge=registration.challenge,
        csr_sha256=parsed.csr_sha256,
    )
    payload = {
        "deployment_id": registration.deployment_id,
        "registration_token": registration.registration_token,
        "challenge": registration.challenge,
        "challenge_proof": proof,
        "csr_pem": csr,
        "agent_version": agent_version,
        "capability_codes": list(capability_codes),
        "hardware_proof_sha256": None,
    }
    try:
        result = client.register(payload)
        certificate_pem, ca_pem = _validate_registration_result(
            result, registration.deployment_id, private_key
        )
        _atomic_bytes(certificate_path, certificate_pem.encode("ascii"), mode=0o600)
        _atomic_bytes(ca_path, ca_pem.encode("ascii"), mode=0o600)
        if not (health_probe or _default_health_probe)():
            raise InstallerRuntimeError("installer.health_failed")
        _record_step(
            root,
            journal_path,
            journal,
            "device_registered",
            {"deployment_id": registration.deployment_id},
        )
        if not client.confirm_initial_snapshot(
            deployment_id=registration.deployment_id,
            certificate_path=certificate_path,
            private_key_path=key_path,
        ):
            raise InstallerRuntimeError("installer.initial_snapshot_failed")
        _record_step(
            root,
            journal_path,
            journal,
            "initial_snapshot",
            {"deployment_id": registration.deployment_id},
        )
        journal.deployment_id = registration.deployment_id
        journal.status = "active"
        journal.current_step = None
        journal.updated_at = _time(datetime.now(UTC))
        _audit(root, journal, "edge.install_completed", "succeeded", "connected")
        _write_journal(journal_path, journal)
        return journal
    except InstallerRuntimeError as exc:
        journal.status = "failed"
        journal.current_step = None
        journal.updated_at = _time(datetime.now(UTC))
        _audit(root, journal, "edge.install_failed", "failed", exc.code)
        _write_journal(journal_path, journal)
        raise
    finally:
        payload.clear()


def uninstall_installation(
    *,
    target_root: Path,
    installation_id: str,
    runner: CommandRunner | None = None,
    purge_config: bool = False,
    purge_audit: bool = False,
    purge_secrets: bool = False,
    confirmation: str = "",
    step_up_proof: str = "",
    expected_step_up_sha256: str = "",
    require_root: bool = True,
) -> InstallJournal:
    """Remove runtime artifacts while retaining all customer-owned data by default."""

    root = _safe_root(target_root)
    _checked_installation_id(installation_id)
    if require_root and (not hasattr(os, "geteuid") or os.geteuid() != 0):
        raise InstallerRuntimeError("installer.root_required")
    journal_path = root / "state" / "installations" / installation_id / "journal.json"
    journal = _load_journal(journal_path)
    purge = [
        name
        for name, selected in (
            ("AUDIT", purge_audit),
            ("CONFIG", purge_config),
            ("SECRETS", purge_secrets),
        )
        if selected
    ]
    expected = f"UNINSTALL {installation_id}"
    if purge:
        expected += " PURGE " + " ".join(purge)
        if (
            not expected_step_up_sha256
            or _SHA256.fullmatch(expected_step_up_sha256) is None
            or not 32 <= len(step_up_proof) <= 512
            or not hmac.compare_digest(
                hashlib.sha256(step_up_proof.encode("utf-8")).hexdigest(),
                expected_step_up_sha256,
            )
        ):
            raise InstallerRuntimeError("installer.step_up_required")
    _require_confirmation(confirmation, expected)
    requested_retention = (not purge_config, not purge_audit, not purge_secrets)
    if journal.status == "uninstalled":
        if (
            journal.retain_config,
            journal.retain_audit,
            journal.retain_secrets,
        ) != requested_retention:
            raise InstallerRuntimeError("installer.state_conflict")
        if purge_audit:
            _finalize_audit_purge(root, journal_path, journal)
        return journal
    journal.status = "uninstalling"
    (
        journal.retain_config,
        journal.retain_audit,
        journal.retain_secrets,
    ) = requested_retention
    journal.updated_at = _time(datetime.now(UTC))
    _write_journal(journal_path, journal)
    release_dir = root / "releases" / journal.release_id
    compose = release_dir / "compose.yaml"
    command_runner = runner or SubprocessCommandRunner()
    try:
        if compose.exists():
            _require_command(
                command_runner,
                ("docker", "compose", "-f", str(compose), "down", "--remove-orphans"),
                timeout=300,
                code="installer.uninstall_failed",
            )
        _remove_tree(release_dir, root)
        if purge_config:
            _remove_tree(root / "config" / installation_id, root)
        if purge_secrets:
            _remove_tree(root / "secrets" / installation_id, root)
        journal.status = "uninstalled"
        journal.current_step = None
        journal.updated_at = _time(datetime.now(UTC))
        _audit(root, journal, "edge.install_completed", "succeeded", "uninstalled")
        _write_journal(journal_path, journal)
        if purge_audit:
            _finalize_audit_purge(root, journal_path, journal)
        return journal
    except InstallerRuntimeError as exc:
        journal.status = "failed"
        journal.updated_at = _time(datetime.now(UTC))
        _audit(root, journal, "edge.install_failed", "failed", exc.code)
        _write_journal(journal_path, journal)
        raise


def _finalize_audit_purge(
    root: Path, journal_path: Path, journal: InstallJournal
) -> None:
    audit = root / "audit" / f"{journal.installation_id}.jsonl"
    try:
        existed = audit.exists() or audit.is_symlink()
        audit.unlink(missing_ok=True)
        if existed:
            _fsync_directory(audit.parent)
    except OSError:
        raise InstallerRuntimeError("installer.uninstall_failed") from None
    if journal.audit_head_sha256 != "0" * 64:
        journal.audit_head_sha256 = "0" * 64
        journal.updated_at = _time(datetime.now(UTC))
        _write_journal(journal_path, journal)


def _install_step(
    root: Path,
    journal_path: Path,
    journal: InstallJournal,
    step: str,
    receipt_value: Mapping[str, object],
    operation: Callable[[], None],
) -> None:
    receipt = _sha({"generation": journal.generation, "step": step, **receipt_value})
    if step in journal.completed_steps:
        if journal.receipts.get(step) != receipt:
            raise InstallerRuntimeError("installer.journal_conflict")
        return
    journal.current_step = step
    journal.updated_at = _time(datetime.now(UTC))
    _write_journal(journal_path, journal)
    operation()
    _record_step(root, journal_path, journal, step, receipt_value)


def _record_step(
    root: Path,
    journal_path: Path,
    journal: InstallJournal,
    step: str,
    receipt_value: Mapping[str, object],
) -> None:
    receipt = _sha({"generation": journal.generation, "step": step, **receipt_value})
    if step in journal.completed_steps:
        if journal.receipts.get(step) != receipt:
            raise InstallerRuntimeError("installer.journal_conflict")
        return
    journal.completed_steps.append(step)
    journal.receipts[step] = receipt
    journal.current_step = step
    journal.updated_at = _time(datetime.now(UTC))
    _audit(root, journal, "edge.install_completed", "succeeded", step, receipt=receipt)
    journal.current_step = None
    _write_journal(journal_path, journal)


def _prepare_staging(path: Path) -> None:
    if path.exists():
        _remove_tree(path, path.parents[1])
    path.mkdir(parents=True, mode=0o700)


def _copy_artifacts(
    artifacts: Sequence[Path], staging: Path, expected_sha256: Mapping[str, str]
) -> None:
    for source in artifacts:
        destination = staging / source.name
        expected = expected_sha256.get(source.name)
        if destination.exists() or destination.is_symlink():
            if (
                destination.is_symlink()
                or not destination.is_file()
                or _file_sha(destination) != expected
            ):
                raise InstallerRuntimeError("installer.artifact_copy_failed")
            continue
        temporary = staging / f".{source.name}.part"
        try:
            temporary.unlink(missing_ok=True)
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            os.chmod(temporary, 0o700 if source.name == "metahub-installer.py" else 0o600)
            if _file_sha(temporary) != expected:
                raise InstallerRuntimeError("installer.artifact_copy_failed")
            os.replace(temporary, destination)
        except InstallerRuntimeError:
            temporary.unlink(missing_ok=True)
            raise
        except OSError:
            temporary.unlink(missing_ok=True)
            raise InstallerRuntimeError("installer.artifact_copy_failed") from None
    _fsync_directory(staging)


def _commit_release(
    staging: Path,
    release_dir: Path,
    verified: VerifiedInstallerRelease,
) -> None:
    if release_dir.exists():
        for artifact in verified.release.manifest.artifacts:
            path = release_dir / artifact.filename
            if not path.is_file() or _file_sha(path) != artifact.sha256:
                raise InstallerRuntimeError("installer.release_conflict")
        if staging.exists():
            _remove_tree(staging, release_dir.parents[1])
        return
    release_dir.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.replace(staging, release_dir)
    _fsync_directory(release_dir.parent)


def _audit(
    root: Path,
    journal: InstallJournal,
    event_type: str,
    outcome: str,
    reason_code: str,
    *,
    receipt: str | None = None,
) -> None:
    audit_dir = root / "audit"
    audit_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = audit_dir / f"{journal.installation_id}.jsonl"
    previous = (
        _verify_audit(path, expected_ancestor=journal.audit_head_sha256)
        if path.exists()
        else "0" * 64
    )
    journal.audit_head_sha256 = previous
    unsigned = {
        "schema_version": _AUDIT_SCHEMA,
        "audit_id": "ina_" + uuid4().hex,
        "installation_id": journal.installation_id,
        "generation": journal.generation,
        "event_type": event_type,
        "step": journal.current_step,
        "outcome": outcome,
        "reason_code": reason_code,
        "receipt_sha256": receipt,
        "previous_sha256": previous,
        "occurred_at": _time(datetime.now(UTC)),
    }
    entry_hash = _sha(unsigned)
    line = canonical_json_bytes({**unsigned, "entry_sha256": entry_hash}) + b"\n"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    journal.audit_head_sha256 = entry_hash


def _verify_audit(path: Path, *, expected_ancestor: str | None = None) -> str:
    try:
        if path.stat().st_size > _MAX_AUDIT_BYTES or path.is_symlink():
            raise ValueError
        previous = "0" * 64
        expected_seen = expected_ancestor in {None, previous}
        with path.open("rb") as stream:
            for raw in stream:
                if len(raw) > 4096:
                    raise ValueError
                value = json.loads(raw.decode("ascii"))
                if type(value) is not dict or value.get("previous_sha256") != previous:
                    raise ValueError
                entry = value.pop("entry_sha256", None)
                if type(entry) is not str or not hmac.compare_digest(entry, _sha(value)):
                    raise ValueError
                previous = entry
                expected_seen = expected_seen or entry == expected_ancestor
        if not expected_seen:
            raise ValueError
        return previous
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise InstallerRuntimeError("installer.audit_integrity_failed") from None


def _load_or_create_key(path: Path) -> Ed25519PrivateKey:
    if path.exists():
        try:
            if path.is_symlink() or path.stat().st_size > 4096:
                raise ValueError
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError
            return key
        except (OSError, TypeError, ValueError):
            raise InstallerRuntimeError("installer.device_key_invalid") from None
    key = Ed25519PrivateKey.generate()
    encoded = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _atomic_bytes(path, encoded, mode=0o600)
    return key


def _csr(private_key: Ed25519PrivateKey) -> str:
    request = (
        x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(private_key, None)
    )
    return request.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _validate_registration_result(
    value: Mapping[str, object], deployment_id: str, private_key: Ed25519PrivateKey
) -> tuple[str, str]:
    expected = {
        "deployment_id",
        "identity",
        "certificate",
        "certificate_pem",
        "ca_certificate_pem",
        "permissions",
        "idempotent_replay",
        "request_id",
        "trace_id",
    }
    try:
        if set(value) != expected or value["deployment_id"] != deployment_id:
            raise ValueError
        certificate_pem = value["certificate_pem"]
        ca_pem = value["ca_certificate_pem"]
        permissions = value["permissions"]
        if (
            type(certificate_pem) is not str
            or type(ca_pem) is not str
            or type(permissions) is not list
            or sorted(permissions)
            != ["edge.self.connect", "edge.self.register", "edge.self.rotate"]
        ):
            raise ValueError
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
        x509.load_pem_x509_certificate(ca_pem.encode("ascii"))
        expected_public = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        actual_public = certificate.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        if actual_public != expected_public:
            raise ValueError
        return certificate_pem, ca_pem
    except (KeyError, TypeError, UnicodeError, ValueError):
        raise InstallerRuntimeError("installer.registration_response_invalid") from None


def _validate_report(
    verified: VerifiedInstallerRelease,
    report: PreflightReport,
) -> None:
    if (
        report.release_id != verified.release.manifest.release_id
        or report.install_profile_id
        not in {item.install_profile_id for item in verified.release.manifest.profiles}
        or report.duration_ms > 120_000
    ):
        raise InstallerRuntimeError("installer.preflight_invalid")


def _same_install(
    journal: InstallJournal,
    verified: VerifiedInstallerRelease,
    report: PreflightReport,
) -> None:
    if (
        journal.release_id != verified.release.manifest.release_id
        or journal.install_profile_id != report.install_profile_id
        or journal.manifest_sha256 != verified.release.manifest_sha256
        or journal.generation != report.generation
    ):
        raise InstallerRuntimeError("installer.journal_conflict")


def _profile(verified: VerifiedInstallerRelease, profile_id: str) -> InstallProfile:
    for item in verified.release.manifest.profiles:
        if item.install_profile_id == profile_id:
            return item
    raise InstallerRuntimeError("installer.profile_not_found")


def _artifact_filename(verified: VerifiedInstallerRelease, kind: str) -> str:
    matches = [item.filename for item in verified.release.manifest.artifacts if item.kind == kind]
    if len(matches) != 1:
        raise InstallerRuntimeError("installer.manifest_invalid")
    return matches[0]


def _load_journal(path: Path) -> InstallJournal:
    try:
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
        return InstallJournal.model_validate(value)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise InstallerRuntimeError("installer.journal_invalid") from None


def _write_journal(path: Path, journal: InstallJournal) -> None:
    _atomic_bytes(path, canonical_json_bytes(journal.model_dump(mode="json")) + b"\n", mode=0o600)


def _atomic_bytes(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _safe_root(value: Path) -> Path:
    try:
        root = value.resolve(strict=False)
    except OSError:
        raise InstallerRuntimeError("installer.root_invalid") from None
    anchor = Path(root.anchor)
    if not root.is_absolute() or root == anchor or len(root.parts) < 3:
        raise InstallerRuntimeError("installer.root_invalid")
    return root


def _remove_tree(path: Path, root: Path) -> None:
    resolved_root = root.resolve(strict=False)
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(resolved_root) or resolved == resolved_root:
        raise InstallerRuntimeError("installer.cleanup_refused")
    if path.exists():
        shutil.rmtree(path)


def _require_command(
    runner: CommandRunner,
    arguments: Sequence[str],
    *,
    timeout: float,
    code: str,
) -> None:
    if runner.run(arguments, timeout=timeout).returncode != 0:
        raise InstallerRuntimeError(code)


def _require_health(probe: Callable[[], bool]) -> None:
    if not probe():
        raise InstallerRuntimeError("installer.health_failed")


def _default_health_probe() -> bool:
    request = urllib.request.Request(
        "http://127.0.0.1:8000/health/ready", headers={"Accept": "application/json"}
    )
    for _attempt in range(30):
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status == 200:
                    response.read(4096)
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    return False


def _control_origin(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname or ""
        local = host in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme not in ({"https", "http"} if local else {"https"})
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        port = parsed.port
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority += f":{port}"
        return f"{parsed.scheme}://{authority}"
    except ValueError:
        raise InstallerRuntimeError("installer.control_url_invalid") from None


def _installation_id(
    root: Path, release_id: str, profile_id: str, generation: int
) -> str:
    selector = hashlib.sha256(
        b"metahub/installation-id/v1\0"
        + str(root).encode("utf-8")
        + b"\0"
        + release_id.encode("ascii")
        + b"\0"
        + profile_id.encode("ascii")
        + b"\0"
        + str(generation).encode("ascii")
    ).hexdigest()
    index = root / "state" / "installation-index" / f"{selector}.id"
    index.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    generated = "ins_" + uuid4().hex
    try:
        descriptor = os.open(index, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write((generated + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(index.parent)
        return generated
    except FileExistsError:
        try:
            if index.is_symlink() or index.stat().st_size != 37:
                raise ValueError
            existing = index.read_text(encoding="ascii").rstrip("\n")
            return _checked_installation_id(existing)
        except (OSError, UnicodeError, ValueError):
            raise InstallerRuntimeError("installer.journal_invalid") from None
    except OSError:
        raise InstallerRuntimeError("installer.journal_write_failed") from None


def _require_confirmation(value: str, expected: str) -> None:
    if len(value) > 256 or not hmac.compare_digest(value, expected):
        raise InstallerRuntimeError("installer.confirmation_required")


def _checked_installation_id(value: str) -> str:
    if _INSTALLATION_ID.fullmatch(value) is None:
        raise InstallerRuntimeError("installer.installation_not_found")
    return value


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        raise InstallerRuntimeError("installer.artifact_missing") from None
    return digest.hexdigest()


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _version(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise InstallerRuntimeError("installer.version_invalid")
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def _bucket(value: int, boundaries: Sequence[int]) -> int:
    return sum(value >= boundary for boundary in boundaries)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = (
    "CommandResult",
    "CommandRunner",
    "InstallJournal",
    "InstallerRuntimeError",
    "RegistrationClient",
    "RegistrationMaterial",
    "SubprocessCommandRunner",
    "SystemProbe",
    "UrlRegistrationClient",
    "connect_installation",
    "install_release",
    "run_preflight",
    "uninstall_installation",
)
