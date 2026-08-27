"""PRD-124 signed release, preflight, journal, recovery, connect, and uninstall tests."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import metahub_shared.installer.runtime as installer_runtime
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from metahub_shared.installer.cli import run_cli
from metahub_shared.installer.release_builder import build_installer_media
from metahub_shared.installer.runtime import (
    CommandResult,
    InstallerRuntimeError,
    RegistrationMaterial,
    connect_installation,
    install_release,
    run_preflight,
    uninstall_installation,
)
from metahub_shared.private_gateway import (
    InstallerContractError,
    load_strict_json,
    verify_installer_artifacts,
    verify_installer_release,
)

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / "infra" / "installers" / "v1.0.0"
MANIFEST = RELEASE_ROOT / "manifest.json"
TRUST_STORE = ROOT / "infra" / "installers" / "trusted-keys.json"
PROFILE_ID = "ipr_12400000000000000000000000000001"
FIXED_NOW = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)


def _trusted() -> dict[str, bytes]:
    document = json.loads(TRUST_STORE.read_text(encoding="utf-8"))
    return {
        item["key_id"]: base64.urlsafe_b64decode(item["public_key"] + "=")
        for item in document["keys"]
    }


def _verified():
    return verify_installer_release(
        load_strict_json(MANIFEST),
        trusted_keys=_trusted(),
        current_schema=116,
        agent_version="1.0.0",
        control_version="1.0.0",
    )


class PassingProbe:
    def __init__(self) -> None:
        self.now = lambda: FIXED_NOW

    def os_name(self) -> str:
        return "linux"

    def architecture(self) -> str:
        return "x86_64"

    def cpu_cores(self) -> int:
        return 8

    def memory_mib(self) -> int:
        return 16_384

    def disk_mib(self, target: Path) -> int:
        del target
        return 102_400

    def ports_available(self, ports) -> bool:
        return list(ports) == [8000, 8090]

    def dns_available(self, targets) -> bool:
        return len(list(targets)) == 2

    def clock_synchronized(self) -> bool:
        return True

    def proxy_valid(self, profile) -> bool:
        return profile.profile_code == "compact"

    def ca_valid(self) -> bool:
        return True

    def filesystem_valid(self, target: Path) -> bool:
        del target
        return True

    def runtime_version(self) -> str:
        return "24.0.7"

    def root_available(self) -> bool:
        return True


class RecordingRunner:
    def __init__(self, *, fail_up_once: bool = False) -> None:
        self.arguments: list[tuple[str, ...]] = []
        self.fail_up_once = fail_up_once

    def run(self, arguments, *, timeout: float) -> CommandResult:
        del timeout
        call = tuple(arguments)
        self.arguments.append(call)
        if self.fail_up_once and "up" in call:
            self.fail_up_once = False
            return CommandResult(1)
        if call[:2] == ("docker", "version"):
            return CommandResult(0, "24.0.7\n")
        return CommandResult(0)


class CrashOnRuntimeStartRunner(RecordingRunner):
    def run(self, arguments, *, timeout: float) -> CommandResult:
        call = tuple(arguments)
        if "up" in call:
            self.arguments.append(call)
            raise SystemExit("simulated process interruption")
        return super().run(arguments, timeout=timeout)


class RegistrationStub:
    def __init__(self, *, snapshot: bool = True) -> None:
        self.register_calls = 0
        self.snapshot_calls = 0
        self.snapshot = snapshot
        self.ca_key = Ed25519PrivateKey.generate()
        self.ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MetaHub Edge CA")])
        now = FIXED_NOW
        self.ca_certificate = (
            x509.CertificateBuilder()
            .subject_name(self.ca_name)
            .issuer_name(self.ca_name)
            .public_key(self.ca_key.public_key())
            .serial_number(1)
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(self.ca_key, None)
        )

    def register(self, payload):
        self.register_calls += 1
        csr = x509.load_pem_x509_csr(str(payload["csr_pem"]).encode("ascii"))
        leaf = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([]))
            .issuer_name(self.ca_name)
            .public_key(csr.public_key())
            .serial_number(2)
            .not_valid_before(FIXED_NOW - timedelta(minutes=1))
            .not_valid_after(FIXED_NOW + timedelta(days=7))
            .sign(self.ca_key, None)
        )
        return {
            "deployment_id": payload["deployment_id"],
            "identity": {},
            "certificate": {},
            "certificate_pem": leaf.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            "ca_certificate_pem": self.ca_certificate.public_bytes(
                serialization.Encoding.PEM
            ).decode("ascii"),
            "permissions": [
                "edge.self.connect",
                "edge.self.register",
                "edge.self.rotate",
            ],
            "idempotent_replay": self.register_calls > 1,
            "request_id": "req_prd124",
            "trace_id": "trc_prd124",
        }

    def confirm_initial_snapshot(self, **kwargs) -> bool:
        self.snapshot_calls += 1
        assert kwargs["certificate_path"].is_file()
        assert kwargs["private_key_path"].is_file()
        return self.snapshot


def _passing_report(target: Path):
    report = run_preflight(
        _verified(),
        install_profile_id=PROFILE_ID,
        artifact_root=RELEASE_ROOT,
        target_root=target,
        probe=PassingProbe(),
    )
    assert report.outcome == "passed"
    return report


def _installed(tmp_path: Path, *, runner: RecordingRunner | None = None):
    target = tmp_path / "metahub-private"
    selected_runner = runner or RecordingRunner()
    journal = install_release(
        _verified(),
        _passing_report(target),
        artifact_root=RELEASE_ROOT,
        target_root=target,
        runner=selected_runner,
        health_probe=lambda: True,
        require_root=False,
    )
    return target, journal, selected_runner


def test_release_verifies_every_artifact_and_fails_closed(tmp_path: Path) -> None:
    verified = _verified()
    assert verified.release.manifest_sha256 == (
        "430dea749d487ce6a3aee1a722e43f202f2a77ce199eca6837e930813598d587"
    )
    assert len(verify_installer_artifacts(verified, RELEASE_ROOT)) == 5

    untrusted = {"other-key": Ed25519PrivateKey.generate().public_key().public_bytes_raw()}
    with pytest.raises(InstallerContractError, match="installer.signing_key_untrusted"):
        verify_installer_release(
            load_strict_json(MANIFEST),
            trusted_keys=untrusted,
            current_schema=116,
            agent_version="1.0.0",
            control_version="1.0.0",
        )
    with pytest.raises(InstallerContractError, match="installer.schema_incompatible"):
        verify_installer_release(
            load_strict_json(MANIFEST),
            trusted_keys=_trusted(),
            current_schema=117,
            agent_version="1.0.0",
            control_version="1.0.0",
        )

    copied = tmp_path / "release"
    shutil.copytree(RELEASE_ROOT, copied)
    (copied / "compose.yaml").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(InstallerContractError, match="installer.artifact_digest_invalid"):
        verify_installer_artifacts(verified, copied)

    unknown = tmp_path / "unknown-release"
    shutil.copytree(RELEASE_ROOT, unknown)
    (unknown / "unlisted.bin").write_bytes(b"not signed\n")
    with pytest.raises(InstallerContractError, match="installer.artifact_invalid"):
        verify_installer_artifacts(verified, unknown)


def test_online_and_offline_media_are_reproducible_from_one_manifest(
    tmp_path: Path,
) -> None:
    first = build_installer_media(
        release_root=RELEASE_ROOT,
        trust_store=TRUST_STORE,
        output_root=tmp_path / "first",
    )
    second = build_installer_media(
        release_root=RELEASE_ROOT,
        trust_store=TRUST_STORE,
        output_root=tmp_path / "second",
    )
    assert first.manifest_sha256 == second.manifest_sha256 == _verified().release.manifest_sha256
    assert first.online_catalog.read_bytes() == second.online_catalog.read_bytes()
    assert first.offline_bundle.read_bytes() == second.offline_bundle.read_bytes()
    assert first.media_manifest.read_bytes() == second.media_manifest.read_bytes()
    online = json.loads(first.online_catalog.read_text(encoding="utf-8"))
    assert online["release"] == load_strict_json(MANIFEST)
    assert len(online["downloads"]) == 5
    with tarfile.open(first.offline_bundle, mode="r:") as archive:
        prefix = "metahub-private-gateway-1.0.0/"
        assert archive.getnames() == [
            prefix + "manifest.json",
            prefix + "trusted-keys.json",
            *(prefix + item["filename"] for item in online["downloads"]),
        ]
        extracted = archive.extractfile(prefix + "manifest.json")
        assert extracted is not None
        assert json.loads(extracted.read()) == load_strict_json(MANIFEST)


def test_preflight_is_pure_read_bounded_and_redacted(tmp_path: Path) -> None:
    target = tmp_path / "not-created" / "install"
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    report = _passing_report(target)
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert before == after == []
    assert report.duration_ms < 120_000
    assert len(report.checks) == 14
    assert {item.outcome for item in report.checks} == {"passed"}
    encoded = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    for forbidden in ("hostname", "127.0.0.1", str(tmp_path), "certificate", "proxy_url"):
        assert forbidden not in encoded


def test_root_install_and_connect_require_exact_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "confirmation" / "metahub-private"
    report = _passing_report(target)
    report_path = tmp_path / "preflight.json"
    report_path.write_text(
        json.dumps(report.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_cli(
        [
            "install",
            "--manifest",
            str(MANIFEST),
            "--artifact-root",
            str(RELEASE_ROOT),
            "--trust-store",
            str(TRUST_STORE),
            "--profile",
            PROFILE_ID,
            "--target-root",
            str(target),
            "--preflight-report",
            str(report_path),
        ],
        stdin=io.StringIO("INSTALL WRONG\n"),
        stdout=stdout,
        stderr=stderr,
        require_root=True,
    )
    assert code == 2
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {
        "status": "error",
        "code": "installer.confirmation_required",
    }
    assert not target.exists()

    installed_target, installed, _runner = _installed(tmp_path / "connect-confirmation")
    material = RegistrationMaterial(
        deployment_id="edp_" + "b" * 32,
        registration_token="erg_" + "C" * 43,
        challenge="ech_" + "D" * 43,
    )
    before = (
        installed_target / "state" / "installations" / installed.installation_id / "journal.json"
    ).read_bytes()
    with pytest.raises(InstallerRuntimeError, match="installer.confirmation_required"):
        connect_installation(
            target_root=installed_target,
            installation_id=installed.installation_id,
            registration=material,
            client=RegistrationStub(),
            agent_version="1.0.0",
            health_probe=lambda: True,
            require_root=True,
        )
    assert (
        installed_target / "state" / "installations" / installed.installation_id / "journal.json"
    ).read_bytes() == before


def test_install_is_idempotent_and_writes_hash_chained_audit(tmp_path: Path) -> None:
    target, journal, runner = _installed(tmp_path)
    assert journal.status == "installed"
    assert journal.compensation_status == "not_required"
    assert journal.completed_steps == list(
        (
            "staging_prepared",
            "artifacts_copied",
            "runtime_validated",
            "runtime_started",
            "health_verified",
            "release_committed",
        )
    )
    release = target / "releases" / journal.release_id
    assert len(verify_installer_artifacts(_verified(), release)) == 5
    calls = len(runner.arguments)
    replay = install_release(
        _verified(),
        _passing_report(target),
        artifact_root=RELEASE_ROOT,
        target_root=target,
        runner=runner,
        health_probe=lambda: True,
        require_root=False,
    )
    assert replay.installation_id == journal.installation_id
    assert len(runner.arguments) == calls
    lines = (
        (target / "audit" / f"{journal.installation_id}.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    )
    assert len(lines) == 8
    previous = "0" * 64
    for line in lines:
        item = json.loads(line)
        assert item["previous_sha256"] == previous
        previous = item["entry_sha256"]
    assert previous == replay.audit_head_sha256


def test_failed_start_compensates_and_same_generation_recovers(tmp_path: Path) -> None:
    target = tmp_path / "metahub-private"
    runner = RecordingRunner(fail_up_once=True)
    report = _passing_report(target)
    with pytest.raises(InstallerRuntimeError, match="installer.runtime_start_failed"):
        install_release(
            _verified(),
            report,
            artifact_root=RELEASE_ROOT,
            target_root=target,
            runner=runner,
            health_probe=lambda: True,
            require_root=False,
        )
    state_file = next((target / "state" / "installations").glob("*/journal.json"))
    failed = json.loads(state_file.read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["compensation_status"] == "completed"
    assert list((target / ".staging").glob("*")) == []

    recovered = install_release(
        _verified(),
        report,
        artifact_root=RELEASE_ROOT,
        target_root=target,
        runner=runner,
        health_probe=lambda: True,
        require_root=False,
    )
    assert recovered.status == "installed"
    assert recovered.generation == 1


def test_process_crash_resumes_receipts_and_post_commit_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "runtime-crash" / "metahub-private"
    report = _passing_report(target)
    with pytest.raises(SystemExit, match="simulated process interruption"):
        install_release(
            _verified(),
            report,
            artifact_root=RELEASE_ROOT,
            target_root=target,
            runner=CrashOnRuntimeStartRunner(),
            health_probe=lambda: True,
            require_root=False,
        )
    interrupted_path = next((target / "state" / "installations").glob("*/journal.json"))
    interrupted = json.loads(interrupted_path.read_text(encoding="utf-8"))
    assert interrupted["status"] == "installing"
    assert interrupted["completed_steps"] == [
        "staging_prepared",
        "artifacts_copied",
        "runtime_validated",
    ]

    resumed_runner = RecordingRunner()
    resumed = install_release(
        _verified(),
        report,
        artifact_root=RELEASE_ROOT,
        target_root=target,
        runner=resumed_runner,
        health_probe=lambda: True,
        require_root=False,
    )
    assert resumed.status == "installed"
    assert not any("config" in call for call in resumed_runner.arguments)
    assert sum("up" in call for call in resumed_runner.arguments) == 1

    commit_target = tmp_path / "commit-crash" / "metahub-private"
    commit_report = _passing_report(commit_target)
    original_record_step = installer_runtime._record_step

    def crash_after_commit(*args, **kwargs):
        step = args[3]
        if step == "release_committed":
            raise SystemExit("simulated commit interruption")
        return original_record_step(*args, **kwargs)

    monkeypatch.setattr(installer_runtime, "_record_step", crash_after_commit)
    with pytest.raises(SystemExit, match="simulated commit interruption"):
        install_release(
            _verified(),
            commit_report,
            artifact_root=RELEASE_ROOT,
            target_root=commit_target,
            runner=RecordingRunner(),
            health_probe=lambda: True,
            require_root=False,
        )
    monkeypatch.setattr(installer_runtime, "_record_step", original_record_step)
    committed = install_release(
        _verified(),
        commit_report,
        artifact_root=RELEASE_ROOT,
        target_root=commit_target,
        runner=RecordingRunner(),
        health_probe=lambda: True,
        require_root=False,
    )
    assert committed.status == "installed"
    assert (commit_target / "releases" / committed.release_id).is_dir()


def test_audit_fsync_ahead_of_journal_recovers_without_chain_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "audit-crash" / "metahub-private"
    report = _passing_report(target)
    original_write_journal = installer_runtime._write_journal
    crashed = False

    def crash_after_first_audit(path, journal):
        nonlocal crashed
        if not crashed and journal.audit_head_sha256 != "0" * 64:
            crashed = True
            raise SystemExit("simulated audit interruption")
        return original_write_journal(path, journal)

    monkeypatch.setattr(installer_runtime, "_write_journal", crash_after_first_audit)
    with pytest.raises(SystemExit, match="simulated audit interruption"):
        install_release(
            _verified(),
            report,
            artifact_root=RELEASE_ROOT,
            target_root=target,
            runner=RecordingRunner(),
            health_probe=lambda: True,
            require_root=False,
        )
    monkeypatch.setattr(installer_runtime, "_write_journal", original_write_journal)
    recovered = install_release(
        _verified(),
        report,
        artifact_root=RELEASE_ROOT,
        target_root=target,
        runner=RecordingRunner(),
        health_probe=lambda: True,
        require_root=False,
    )
    audit_path = target / "audit" / f"{recovered.installation_id}.jsonl"
    audit_rows = [json.loads(line) for line in audit_path.read_text(encoding="ascii").splitlines()]
    assert recovered.status == "installed"
    assert audit_rows[0]["reason_code"] == "install_started"
    assert audit_rows[1]["previous_sha256"] == audit_rows[0]["entry_sha256"]
    assert recovered.audit_head_sha256 == audit_rows[-1]["entry_sha256"]


def test_connect_reuses_identity_and_uninstall_retains_customer_data(tmp_path: Path) -> None:
    target, installed, runner = _installed(tmp_path)
    deployment_id = "edp_" + "a" * 32
    material = RegistrationMaterial(
        deployment_id=deployment_id,
        registration_token="erg_" + "A" * 43,
        challenge="ech_" + "B" * 43,
    )
    client = RegistrationStub()
    connected = connect_installation(
        target_root=target,
        installation_id=installed.installation_id,
        registration=material,
        client=client,
        agent_version="1.0.0",
        health_probe=lambda: True,
        require_root=False,
    )
    key_path = target / "secrets" / installed.installation_id / "device-key.pem"
    key_sha = hashlib.sha256(key_path.read_bytes()).hexdigest()
    replay = connect_installation(
        target_root=target,
        installation_id=installed.installation_id,
        registration=material,
        client=client,
        agent_version="1.0.0",
        health_probe=lambda: True,
        require_root=False,
    )
    assert connected.status == replay.status == "active"
    assert client.register_calls == client.snapshot_calls == 1
    assert hashlib.sha256(key_path.read_bytes()).hexdigest() == key_sha

    config = target / "config" / installed.installation_id
    config.mkdir(parents=True)
    (config / "retained.json").write_text("{}\n", encoding="utf-8")
    confirmation = f"UNINSTALL {installed.installation_id}"
    removed = uninstall_installation(
        target_root=target,
        installation_id=installed.installation_id,
        runner=runner,
        confirmation=confirmation,
        require_root=False,
    )
    assert removed.status == "uninstalled"
    assert removed.retain_config and removed.retain_audit and removed.retain_secrets
    assert config.is_dir() and key_path.is_file()
    assert not (target / "releases" / installed.release_id).exists()


def test_purge_requires_step_up_and_exact_per_class_confirmation(tmp_path: Path) -> None:
    target, installed, runner = _installed(tmp_path)
    secret_dir = target / "secrets" / installed.installation_id
    secret_dir.mkdir(parents=True)
    (secret_dir / "local.bin").write_bytes(b"local-only")
    config_dir = target / "config" / installed.installation_id
    config_dir.mkdir(parents=True)
    proof = "local-step-up-proof-value-000001"
    digest = hashlib.sha256(proof.encode()).hexdigest()
    with pytest.raises(InstallerRuntimeError, match="installer.step_up_required"):
        uninstall_installation(
            target_root=target,
            installation_id=installed.installation_id,
            runner=runner,
            purge_config=True,
            confirmation=f"UNINSTALL {installed.installation_id} PURGE CONFIG",
            require_root=False,
        )
    removed = uninstall_installation(
        target_root=target,
        installation_id=installed.installation_id,
        runner=runner,
        purge_config=True,
        purge_audit=True,
        purge_secrets=True,
        confirmation=f"UNINSTALL {installed.installation_id} PURGE AUDIT CONFIG SECRETS",
        step_up_proof=proof,
        expected_step_up_sha256=digest,
        require_root=False,
    )
    assert not config_dir.exists() and not secret_dir.exists()
    assert not (target / "audit" / f"{installed.installation_id}.jsonl").exists()
    assert not removed.retain_config and not removed.retain_audit and not removed.retain_secrets


def test_audit_purge_recovers_both_terminal_journal_crash_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = "local-step-up-proof-value-000001"
    digest = hashlib.sha256(proof.encode()).hexdigest()
    original_write_journal = installer_runtime._write_journal

    before_delete_target, before_delete_install, before_delete_runner = _installed(
        tmp_path / "before-audit-delete"
    )
    before_delete_audit = (
        before_delete_target / "audit" / f"{before_delete_install.installation_id}.jsonl"
    )
    crashed_after_terminal_write = False

    def crash_after_terminal_write(path, journal):
        nonlocal crashed_after_terminal_write
        original_write_journal(path, journal)
        if (
            not crashed_after_terminal_write
            and journal.status == "uninstalled"
            and not journal.retain_audit
            and journal.audit_head_sha256 != "0" * 64
        ):
            crashed_after_terminal_write = True
            raise SystemExit("simulated pre-audit-delete interruption")

    monkeypatch.setattr(installer_runtime, "_write_journal", crash_after_terminal_write)
    before_confirmation = f"UNINSTALL {before_delete_install.installation_id} PURGE AUDIT"
    with pytest.raises(SystemExit, match="simulated pre-audit-delete interruption"):
        uninstall_installation(
            target_root=before_delete_target,
            installation_id=before_delete_install.installation_id,
            runner=before_delete_runner,
            purge_audit=True,
            confirmation=before_confirmation,
            step_up_proof=proof,
            expected_step_up_sha256=digest,
            require_root=False,
        )
    assert before_delete_audit.is_file()
    monkeypatch.setattr(installer_runtime, "_write_journal", original_write_journal)
    recovered_before_delete = uninstall_installation(
        target_root=before_delete_target,
        installation_id=before_delete_install.installation_id,
        runner=before_delete_runner,
        purge_audit=True,
        confirmation=before_confirmation,
        step_up_proof=proof,
        expected_step_up_sha256=digest,
        require_root=False,
    )
    assert not before_delete_audit.exists()
    assert recovered_before_delete.audit_head_sha256 == "0" * 64

    after_delete_target, after_delete_install, after_delete_runner = _installed(
        tmp_path / "after-audit-delete"
    )
    after_delete_audit = (
        after_delete_target / "audit" / f"{after_delete_install.installation_id}.jsonl"
    )
    crashed_before_head_write = False

    def crash_before_zero_head_write(path, journal):
        nonlocal crashed_before_head_write
        if (
            not crashed_before_head_write
            and journal.status == "uninstalled"
            and not journal.retain_audit
            and journal.audit_head_sha256 == "0" * 64
        ):
            crashed_before_head_write = True
            raise SystemExit("simulated post-audit-delete interruption")
        original_write_journal(path, journal)

    monkeypatch.setattr(installer_runtime, "_write_journal", crash_before_zero_head_write)
    after_confirmation = f"UNINSTALL {after_delete_install.installation_id} PURGE AUDIT"
    with pytest.raises(SystemExit, match="simulated post-audit-delete interruption"):
        uninstall_installation(
            target_root=after_delete_target,
            installation_id=after_delete_install.installation_id,
            runner=after_delete_runner,
            purge_audit=True,
            confirmation=after_confirmation,
            step_up_proof=proof,
            expected_step_up_sha256=digest,
            require_root=False,
        )
    assert not after_delete_audit.exists()
    persisted = json.loads(
        (
            after_delete_target
            / "state"
            / "installations"
            / after_delete_install.installation_id
            / "journal.json"
        ).read_text(encoding="utf-8")
    )
    assert persisted["audit_head_sha256"] != "0" * 64
    monkeypatch.setattr(installer_runtime, "_write_journal", original_write_journal)
    recovered_after_delete = uninstall_installation(
        target_root=after_delete_target,
        installation_id=after_delete_install.installation_id,
        runner=after_delete_runner,
        purge_audit=True,
        confirmation=after_confirmation,
        step_up_proof=proof,
        expected_step_up_sha256=digest,
        require_root=False,
    )
    assert recovered_after_delete.audit_head_sha256 == "0" * 64


def test_cli_never_echoes_registration_material_on_invalid_input(tmp_path: Path) -> None:
    sensitive = "erg_" + "Z" * 43
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_cli(
        [
            "connect",
            "--target-root",
            str(tmp_path / "root"),
            "--installation-id",
            "ins_" + "1" * 32,
            "--registration-stdin",
        ],
        stdin=io.StringIO(
            json.dumps(
                {
                    "deployment_id": "edp_" + "1" * 32,
                    "registration_token": sensitive,
                    "challenge": "ech_" + "X" * 43,
                    "unknown": sensitive,
                }
            )
        ),
        stdout=stdout,
        stderr=stderr,
        require_root=False,
    )
    assert code == 2
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {
        "status": "error",
        "code": "installer.registration_invalid",
    }
    assert sensitive not in stderr.getvalue()
