"""Secret-safe `metahub` CLI for PRD-124 preflight and installation lifecycle."""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.private_gateway import (
    EdgeLicenseStore,
    InstallerContractError,
    LicenseContractError,
    OfflineBundleContractError,
    OfflineBundleStateStore,
    PreflightReport,
    load_strict_json,
    verify_installer_release,
    verify_license_entitlement,
    verify_offline_bundle_archive,
)

from .runtime import (
    CommandRunner,
    InstallerRuntimeError,
    RegistrationClient,
    RegistrationMaterial,
    SubprocessCommandRunner,
    SystemProbe,
    UrlRegistrationClient,
    connect_installation,
    install_release,
    run_preflight,
    uninstall_installation,
)

_MAX_STDIN_BYTES = 64 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")


class CliFailure(Exception):
    def __init__(self, code: str, exit_code: int) -> None:
        checked = code if _SAFE_CODE.fullmatch(code) else "installer.failed"
        super().__init__(checked)
        self.code = checked
        self.exit_code = exit_code


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise CliFailure("installer.argument_invalid", 2)


def _build_parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(prog="metahub", description="MetaHub private gateway installer")
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_SafeArgumentParser
    )

    preflight = commands.add_parser("preflight")
    _release_arguments(preflight)
    preflight.add_argument("--report-output")
    preflight.add_argument("--custom-ca")

    install = commands.add_parser("install")
    _release_arguments(install)
    install.add_argument("--preflight-report", required=True)

    connect = commands.add_parser("connect")
    connect.add_argument("--target-root", default="/opt/metahub")
    connect.add_argument("--installation-id", required=True)
    connect.add_argument("--control-url", default="https://api.metahub.example")
    connect.add_argument("--registration-stdin", action="store_true")
    connect.add_argument("--registration-input")
    connect.add_argument("--agent-version", default="1.0.0")

    license_command = commands.add_parser("license")
    license_actions = license_command.add_subparsers(
        dest="license_action", required=True, parser_class=_SafeArgumentParser
    )
    license_import = license_actions.add_parser("import")
    source = license_import.add_mutually_exclusive_group(required=True)
    source.add_argument("--input")
    source.add_argument("--stdin", action="store_true", dest="input_stdin")
    license_import.add_argument("--trust-store", required=True)
    license_import.add_argument("--state-root", required=True)
    license_import.add_argument("--workspace-id", required=True)
    license_import.add_argument("--tenant-id", required=True)
    license_import.add_argument("--deployment-id", required=True)
    license_import.add_argument("--device-generation", type=int, required=True)

    bundle_command = commands.add_parser("bundle")
    bundle_actions = bundle_command.add_subparsers(
        dest="bundle_action", required=True, parser_class=_SafeArgumentParser
    )
    bundle_verify = bundle_actions.add_parser("verify")
    _offline_bundle_arguments(bundle_verify)
    bundle_apply = bundle_actions.add_parser("apply")
    _offline_bundle_arguments(bundle_apply)
    bundle_apply.add_argument("--state-root", required=True)
    bundle_apply.add_argument("--confirmation-stdin", action="store_true")

    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--target-root", default="/opt/metahub")
    uninstall.add_argument("--installation-id", required=True)
    uninstall.add_argument("--purge-config", action="store_true")
    uninstall.add_argument("--purge-audit", action="store_true")
    uninstall.add_argument("--purge-secrets", action="store_true")
    uninstall.add_argument("--confirmation-stdin", action="store_true")
    return parser


def _release_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--trust-store", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--target-root", default="/opt/metahub")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--schema-version", type=int, default=116)
    parser.add_argument("--agent-version", default="1.0.0")
    parser.add_argument("--control-version", default="1.0.0")


def _offline_bundle_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", required=True)
    parser.add_argument("--trust-store", required=True)
    parser.add_argument("--staging-root", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--direction", required=True, choices=("cloud_to_edge", "edge_to_cloud"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--key-input")
    source.add_argument("--key-stdin", action="store_true")


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
    probe_factory: Callable[[argparse.Namespace, CommandRunner, Mapping[str, str]], SystemProbe]
    | None = None,
    client_factory: Callable[[str], RegistrationClient] = UrlRegistrationClient,
    health_probe: Callable[[], bool] | None = None,
    require_root: bool = True,
) -> int:
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    environment = environ if environ is not None else os.environ
    command_runner = runner or SubprocessCommandRunner()
    try:
        arguments = _build_parser().parse_args(list(argv) if argv is not None else None)
        if arguments.command == "license":
            return _import_license(arguments, input_stream, output_stream)
        if arguments.command == "bundle":
            return _offline_bundle(arguments, input_stream, output_stream, error_stream)
        if arguments.command in {"preflight", "install"}:
            verified = _verified_release(arguments)
        if arguments.command == "preflight":
            factory = probe_factory or _system_probe
            report = run_preflight(
                verified,
                install_profile_id=arguments.profile,
                artifact_root=Path(arguments.artifact_root),
                target_root=Path(arguments.target_root),
                generation=_generation(arguments.generation),
                probe=factory(arguments, command_runner, environment),
            )
            payload = report.model_dump(mode="json")
            if arguments.report_output:
                _write_private_json(Path(arguments.report_output), payload)
            _emit(output_stream, payload)
            return 0 if report.outcome == "passed" else 3
        if arguments.command == "install":
            report = _preflight_report(Path(arguments.preflight_report))
            confirmation = (
                _read_action_confirmation(
                    input_stream,
                    error_stream,
                    f"INSTALL {verified.release.manifest.release_id} "
                    f"PROFILE {report.install_profile_id} GENERATION {report.generation}",
                )
                if require_root
                else ""
            )
            journal = install_release(
                verified,
                report,
                artifact_root=Path(arguments.artifact_root),
                target_root=Path(arguments.target_root),
                runner=command_runner,
                health_probe=health_probe,
                confirmation=confirmation,
                require_root=require_root,
            )
            _emit(output_stream, _journal_summary(journal))
            return 0
        if arguments.command == "connect":
            if arguments.registration_stdin == bool(arguments.registration_input):
                raise CliFailure("installer.argument_invalid", 2)
            confirmation = (
                _read_action_confirmation(
                    input_stream,
                    error_stream,
                    f"CONNECT {arguments.installation_id}",
                )
                if require_root
                else ""
            )
            registration = (
                _registration(_read_stdin_json(input_stream))
                if arguments.registration_stdin
                else _registration(_read_private_json(Path(arguments.registration_input)))
            )
            journal = connect_installation(
                target_root=Path(arguments.target_root),
                installation_id=arguments.installation_id,
                registration=registration,
                client=client_factory(arguments.control_url),
                agent_version=arguments.agent_version,
                health_probe=health_probe,
                confirmation=confirmation,
                require_root=require_root,
            )
            _emit(output_stream, _journal_summary(journal))
            return 0
        if not arguments.confirmation_stdin:
            raise CliFailure("installer.confirmation_required", 2)
        confirmation = _confirmation(_read_stdin_json(input_stream))
        journal = uninstall_installation(
            target_root=Path(arguments.target_root),
            installation_id=arguments.installation_id,
            runner=command_runner,
            purge_config=arguments.purge_config,
            purge_audit=arguments.purge_audit,
            purge_secrets=arguments.purge_secrets,
            confirmation=confirmation["confirmation"],
            step_up_proof=confirmation["step_up_proof"],
            expected_step_up_sha256=environment.get("METAHUB_INSTALLER_STEP_UP_SHA256", ""),
            require_root=require_root,
        )
        _emit(output_stream, _journal_summary(journal))
        return 0
    except CliFailure as failure:
        _emit(error_stream, {"status": "error", "code": failure.code})
        return failure.exit_code
    except InstallerContractError as failure:
        _emit(error_stream, {"status": "error", "code": failure.code})
        return 4
    except LicenseContractError as failure:
        _emit(error_stream, {"status": "error", "code": failure.code})
        return 4
    except OfflineBundleContractError as failure:
        _emit(error_stream, {"status": "error", "code": failure.code})
        return 4
    except InstallerRuntimeError as failure:
        _emit(error_stream, {"status": "error", "code": failure.code})
        return 5
    except Exception:
        _emit(error_stream, {"status": "error", "code": "installer.failed"})
        return 5


def _verified_release(arguments: argparse.Namespace):
    manifest = load_strict_json(Path(arguments.manifest))
    trusted = _trusted_keys(Path(arguments.trust_store))
    return verify_installer_release(
        manifest,
        trusted_keys=trusted,
        current_schema=arguments.schema_version,
        agent_version=arguments.agent_version,
        control_version=arguments.control_version,
    )


def _trusted_keys(path: Path) -> dict[str, bytes]:
    return _purpose_trusted_keys(
        path,
        schema_version="metahub.installer-trust/v1",
        purpose="metahub.edge-installer.release/v1",
        error_code="installer.trust_store_invalid",
    )


def _license_trusted_keys(path: Path) -> dict[str, bytes]:
    return _purpose_trusted_keys(
        path,
        schema_version="metahub.edge-license-trust/v1",
        purpose="metahub.edge-license.entitlement/v1",
        error_code="edge_license.trust_store_invalid",
    )


def _offline_trusted_keys(path: Path) -> dict[str, bytes]:
    return _purpose_trusted_keys(
        path,
        schema_version="metahub.edge-offline-trust/v1",
        purpose="metahub.edge-offline-bundle.manifest/v1",
        error_code="edge_offline_bundle.key_untrusted",
    )


def _purpose_trusted_keys(
    path: Path,
    *,
    schema_version: str,
    purpose: str,
    error_code: str,
) -> dict[str, bytes]:
    try:
        value = load_strict_json(path, maximum_bytes=64 * 1024)
        if type(value) is not dict or set(value) != {"schema_version", "keys"}:
            raise ValueError
        if value["schema_version"] != schema_version or type(value["keys"]) is not list:
            raise ValueError
        result: dict[str, bytes] = {}
        for item in value["keys"]:
            if (
                type(item) is not dict
                or set(item) != {"key_id", "algorithm", "public_key", "purpose", "status"}
                or item["algorithm"] != "Ed25519"
                or item["purpose"] != purpose
                or item["status"] != "active"
                or type(item["key_id"]) is not str
                or item["key_id"] in result
                or type(item["public_key"]) is not str
                or "=" in item["public_key"]
            ):
                raise ValueError
            decoded = base64.b64decode(
                item["public_key"] + "=" * ((4 - len(item["public_key"]) % 4) % 4),
                altchars=b"-_",
                validate=True,
            )
            if len(decoded) != 32:
                raise ValueError
            result[item["key_id"]] = decoded
        if not result:
            raise ValueError
        return result
    except (InstallerContractError, KeyError, TypeError, ValueError):
        raise CliFailure(error_code, 4) from None


def _import_license(
    arguments: argparse.Namespace,
    input_stream: TextIO,
    output_stream: TextIO,
) -> int:
    document = (
        _read_stdin_json(input_stream)
        if arguments.input_stdin
        else _read_private_json(Path(arguments.input))
    )
    trusted_keys = _license_trusted_keys(Path(arguments.trust_store))
    verified = verify_license_entitlement(document, trusted_keys=trusted_keys)
    entitlement = verified.signed.entitlement
    if (
        entitlement.workspace_id != arguments.workspace_id
        or entitlement.tenant_id != arguments.tenant_id
        or entitlement.deployment_id != arguments.deployment_id
        or entitlement.device_generation != _generation(arguments.device_generation)
    ):
        raise LicenseContractError("edge_license.owner_mismatch")
    store = EdgeLicenseStore(
        Path(arguments.state_root),
        workspace_id=arguments.workspace_id,
        tenant_id=arguments.tenant_id,
        deployment_id=arguments.deployment_id,
        device_generation=arguments.device_generation,
        trusted_keys=trusted_keys,
    )
    now = datetime.now(UTC)
    changed = store.apply(document, now=now)
    decision = store.decision(now=now)
    _emit(
        output_stream,
        {
            "status": "applied" if changed else "not_modified",
            "license_id": entitlement.license_id,
            "deployment_id": entitlement.deployment_id,
            "generation": entitlement.generation,
            "decision_status": decision.status,
            "manifest_sha256": verified.signed.manifest_sha256,
            "expires_at": _optional_time(decision.expires_at),
            "grace_ends_at": _optional_time(decision.grace_ends_at),
        },
    )
    return 0


def _offline_bundle(
    arguments: argparse.Namespace,
    input_stream: TextIO,
    output_stream: TextIO,
    error_stream: TextIO,
) -> int:
    if arguments.bundle_action == "apply" and arguments.key_stdin:
        raise CliFailure("installer.argument_invalid", 2)
    key_document = (
        _read_stdin_json(input_stream)
        if arguments.key_stdin
        else _read_private_json(Path(arguments.key_input))
    )
    content_key = _offline_content_key(key_document)
    trusted_keys = _offline_trusted_keys(Path(arguments.trust_store))
    verified = verify_offline_bundle_archive(
        Path(arguments.input),
        Path(arguments.staging_root),
        trusted_keys=trusted_keys,
        unwrap_key=lambda _envelope: content_key,
        expected_workspace_id=arguments.workspace_id,
        expected_tenant_id=arguments.tenant_id,
        expected_deployment_id=arguments.deployment_id,
        expected_direction=arguments.direction,
        now=datetime.now(UTC),
    )
    manifest = verified.verified.signed.manifest
    if arguments.bundle_action == "verify":
        _emit(
            output_stream,
            {
                "bundle_id": manifest.bundle_id,
                "direction": manifest.direction,
                "domains": [item.domain for item in manifest.version_vector],
                "entry_count": len(manifest.entries),
                "expires_at": _optional_time(manifest.expires_at),
                "manifest_sha256": verified.verified.signed.manifest_sha256,
                "producer_generation": manifest.producer_generation,
                "status": "verified",
            },
        )
        return 0
    if not arguments.confirmation_stdin:
        raise CliFailure("installer.confirmation_required", 2)
    _read_action_confirmation(
        input_stream,
        error_stream,
        f"APPLY {manifest.bundle_id} TO {manifest.deployment_id}",
    )
    store = OfflineBundleStateStore(
        Path(arguments.state_root),
        workspace_id=arguments.workspace_id,
        tenant_id=arguments.tenant_id,
        deployment_id=arguments.deployment_id,
    )
    result = store.apply(verified)
    _emit(
        output_stream,
        {
            "bundle_id": result.bundle_id,
            "domains": list(result.domains),
            "manifest_sha256": result.manifest_sha256,
            "producer_generation": result.producer_generation,
            "status": "applied" if result.changed else "not_modified",
        },
    )
    return 0


def _offline_content_key(value: object) -> bytes:
    if (
        type(value) is not dict
        or set(value) != {"content_key", "schema_version"}
        or value["schema_version"] != "metahub.edge-offline-media-key/v1"
        or type(value["content_key"]) is not str
        or "=" in value["content_key"]
    ):
        raise CliFailure("edge_offline_bundle.key_invalid", 4)
    try:
        decoded = base64.b64decode(
            value["content_key"] + "=" * ((4 - len(value["content_key"]) % 4) % 4),
            altchars=b"-_",
            validate=True,
        )
    except ValueError:
        raise CliFailure("edge_offline_bundle.key_invalid", 4) from None
    if len(decoded) != 32:
        raise CliFailure("edge_offline_bundle.key_invalid", 4)
    return decoded


def _preflight_report(path: Path) -> PreflightReport:
    try:
        return PreflightReport.model_validate(load_strict_json(path, maximum_bytes=128 * 1024))
    except (InstallerContractError, ValueError):
        raise CliFailure("installer.preflight_invalid", 2) from None


def _registration(value: object) -> RegistrationMaterial:
    try:
        return RegistrationMaterial.model_validate(value)
    except ValueError:
        raise CliFailure("installer.registration_invalid", 2) from None


def _confirmation(value: object) -> dict[str, str]:
    if (
        type(value) is not dict
        or set(value) != {"confirmation", "step_up_proof"}
        or type(value["confirmation"]) is not str
        or type(value["step_up_proof"]) is not str
        or len(value["confirmation"]) > 256
        or len(value["step_up_proof"]) > 512
    ):
        raise CliFailure("installer.confirmation_required", 2)
    return value


def _read_stdin_json(stream: TextIO) -> object:
    try:
        value = stream.read(_MAX_STDIN_BYTES + 1)
        if not value or len(value.encode("utf-8")) > _MAX_STDIN_BYTES:
            raise ValueError
        return _loads_exact(value)
    except (UnicodeError, ValueError):
        raise CliFailure("installer.input_invalid", 2) from None


def _read_action_confirmation(stream: TextIO, prompt_stream: TextIO, expected: str) -> str:
    if stream.isatty():
        prompt_stream.write(f"Type '{expected}' to continue: ")
        prompt_stream.flush()
    try:
        value = stream.readline(258)
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        if (
            not value
            or len(value.encode("utf-8")) > 256
            or "\r" in value
            or "\n" in value
            or not hmac.compare_digest(value, expected)
        ):
            raise ValueError
        return value
    except (UnicodeError, ValueError):
        raise CliFailure("installer.confirmation_required", 2) from None


def _read_private_json(path: Path) -> object:
    try:
        info = path.stat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_STDIN_BYTES:
            raise ValueError
        if os.name != "nt" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError
        return _loads_exact(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise CliFailure("installer.input_invalid", 2) from None


def _loads_exact(value: str) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    return json.loads(
        value,
        object_pairs_hook=pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )


def _write_private_json(path: Path, value: object) -> None:
    if path.exists():
        raise CliFailure("installer.output_exists", 2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(canonical_json_bytes(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CliFailure("installer.output_failed", 5) from None


def _journal_summary(journal) -> dict[str, object]:
    return {
        "status": journal.status,
        "installation_id": journal.installation_id,
        "release_id": journal.release_id,
        "install_profile_id": journal.install_profile_id,
        "generation": journal.generation,
        "deployment_id": journal.deployment_id,
        "compensation_status": journal.compensation_status,
        "retain_config": journal.retain_config,
        "retain_audit": journal.retain_audit,
        "retain_secrets": journal.retain_secrets,
    }


def _system_probe(
    arguments: argparse.Namespace,
    runner: CommandRunner,
    environ: Mapping[str, str],
) -> SystemProbe:
    return SystemProbe(
        runner=runner,
        environ=environ,
        custom_ca=Path(arguments.custom_ca) if arguments.custom_ca else None,
    )


def _generation(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise CliFailure("installer.argument_invalid", 2)
    return value


def _optional_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _emit(stream: TextIO, value: object) -> None:
    stream.write(canonical_json_bytes(value).decode("ascii") + "\n")
    stream.flush()


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
