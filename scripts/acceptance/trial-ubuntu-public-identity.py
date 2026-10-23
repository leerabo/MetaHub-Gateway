"""Measure public identity setup on the private Ubuntu fixture.

No authentication, cookies, database edits, TLS bypass or public server access.
Require a separately verified recovery or explicit private runtime receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import re
import socket
import ssl
import time

ROOT = Path(__file__).resolve().parents[2]
REMOTE_STATE = "/opt/metahub-trial-cold-recovery-trial8-20261017"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', help='Require this exact serving release')
    runtime = parser.add_mutually_exclusive_group()
    runtime.add_argument('--diagnostic-runtime', action='store_true',
                        help='Use a separately verified private runtime; never imply cold recovery passed')
    runtime.add_argument('--manual-runtime', action='store_true',
                        help='Require a successful normal-deadline private activation receipt')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.release and not re.fullmatch(r'2\.0\.0-trial\.[1-9][0-9]*', args.release):
        parser.error('Only a numbered independent trial release is allowed')
    if (args.diagnostic_runtime or args.manual_runtime) and not args.release:
        parser.error('Private runtime observations require an exact release')
    if args.output and args.output.exists():
        raise ValueError('Retain existing observations; choose a new output path')
    if args.manual_runtime:
        number = args.release.rsplit('.', 1)[1]
        guards = [
            "assert json.loads((state/'recovery.json').read_text())['status']=='failed_activation_requires_review'",
            f"receipt=json.loads(Path('/home/acceptance/trial{number}-reviewed-manual-activation-result.json').read_text())",
            "assert receipt['status']=='reviewed_manual_forward_activation_passed'",
            f"assert receipt['active_release']=={args.release!r}",
            "assert receipt['production_startup_deadline_changed'] is False",
            "assert all(receipt['preservation_after'].values()) and receipt['actual_ca_verified_https_version']",
            "assert receipt['original_recovery_failure_preserved'] and receipt['original_saas_failure_preserved']",
            f"assert (state/'current-release').read_text().strip()=='/opt/metahub-trial-upgrade-store-lifecycle-recovery/{args.release}'",
        ]
    elif args.diagnostic_runtime:
        number = args.release.rsplit('.', 1)[1]
        guards = [
            "assert json.loads((state/'recovery.json').read_text())['status']=='failed_activation_requires_review'",
            f"diagnostic=json.loads(Path('/home/acceptance/trial{number}-tcg-runtime-diagnostic-result.json').read_text())",
            "assert diagnostic['status']=='manual_runtime_diagnostic_passed' and diagnostic['diagnostic_only'] is True",
            f"assert diagnostic['active_release']=={args.release!r}",
            "assert all(diagnostic['preservation_after'].values()) and diagnostic['actual_ca_verified_https_version']",
            "assert diagnostic['original_recovery_failure_preserved'] and not (state/'current-release').exists()",
        ]
    else:
        guards = [
            "assert json.loads((state/'recovery.json').read_text())['status']=='recovered'",
            "proof=json.loads(Path('/home/acceptance/trial8-cold-recovery-preservation.json').read_text())",
            "assert proof['status']=='passed' and all(proof['checks'].values())",
        ]
    spec = importlib.util.spec_from_file_location(
        "ubuntu_ssh", ROOT / "scripts/acceptance/trial-ubuntu-ssh.py")
    ssh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ssh)
    client = ssh.connect()
    try:
        # Fixed fixture paths, never an arbitrary supplied server or state.
        command = "sudo python3 - <<'PY'\n" + "\n".join([
            "from pathlib import Path",
            "import json",
            f"state=Path({REMOTE_STATE!r})",
            *guards,
            "assert 'METAHUB_DOMAIN=trial.local' in (state/'compose.env').read_text()",
            "print((state/'authority/ca.crt').read_text(),end='')",
        ]) + "\nPY"
        _, output, _ = client.exec_command(command, timeout=120)
        ca = output.read().decode("ascii")
        if output.channel.recv_exit_status() or not ca.startswith("-----BEGIN CERTIFICATE-----"):
            raise ValueError("Selected fixture preservation is not established")
    finally:
        client.close()
    context = ssl.create_default_context(cadata=ca)
    observations = []
    version = None
    for path in ("/version", *("/identity/registration-policy" for _ in range(3))):
        begin = time.monotonic()
        connection = http.client.HTTPSConnection("trial.local", 8446, context=context, timeout=40)
        try:
            raw = socket.create_connection(("127.0.0.1", 8446), timeout=20)
            connection.sock = context.wrap_socket(raw, server_hostname="trial.local")
            connection.request("GET", path, headers={
                "Host": "trial.local:8446", "Connection": "close",
                "User-Agent": "MetaHub-private-public-identity-acceptance",
            })
            response = connection.getresponse()
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError("Unexpected response size")
            row = dict(path=path, status=response.status,
                       elapsed_seconds=round(time.monotonic() - begin, 3),
                       bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                       cache_control=response.getheader("Cache-Control"))
            cache_directives = {
                directive.strip().split('=', 1)[0].lower()
                for directive in (row['cache_control'] or '').split(',')
            }
            row['cache_storage_disabled'] = 'no-store' in cache_directives
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                payload = None
            if path == "/version" and response.status == 200:
                candidate = payload.get("version") if isinstance(payload, dict) else None
                if candidate not in ({args.release} if args.release else {"2.0.0-trial.6", "2.0.0-trial.8"}):
                    raise ValueError("Unexpected serving fixture release")
                version = candidate
            if path == "/identity/registration-policy" and response.status == 200:
                expected = {"email_verification", "human_verification", "revision",
                            "email_delivery_available", "local_registration"}
                if (not isinstance(payload, dict) or set(payload) != expected
                        or not all(type(payload[k]) is bool for k in expected - {"revision"})
                        or type(payload["revision"]) is not int):
                    raise ValueError("Unexpected public registration policy contract")
                row["production_email_verification_required"] = payload["email_verification"] is True
                row["local_registration_disabled"] = payload["local_registration"] is False
                row["smtp_available"] = payload["email_delivery_available"]
                row["within_frontend_setup_deadline"] = row["elapsed_seconds"] < 8
            elif isinstance(payload, dict):
                code = payload.get("error", {}).get("code") if isinstance(payload.get("error"), dict) else None
                if isinstance(code, str) and re.fullmatch(r"[a-z0-9_.-]{1,100}", code):
                    row["error_code"] = code
            observations.append(row)
        except (OSError, TimeoutError, ValueError) as error:
            observations.append(dict(path=path, status="transport_or_validation_failed",
                                     elapsed_seconds=round(time.monotonic() - begin, 3),
                                     exception_type=type(error).__name__))
        finally:
            connection.close()
    policies = [row for row in observations if row["path"] == "/identity/registration-policy"]
    passed = version is not None and len(policies) == 3 and all(
        row["status"] == 200 and row.get("production_email_verification_required")
        and row.get("local_registration_disabled") and row.get("smtp_available")
        and row.get("within_frontend_setup_deadline")
        and row.get("cache_storage_disabled") is True for row in policies)
    report = dict(status="passed" if passed else "failed_requires_review", release=version,
                  observed_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  certificate_chain_and_hostname_verified=True, authentication_attempted=False,
                  customer_database_used=False, observations=observations,
                  diagnostic_runtime_only=args.diagnostic_runtime,
                  normal_deadline_manual_runtime=args.manual_runtime,
                  complete_cold_recovery_verified_by_this_probe=False,
                  fixture_recovery_status='failed_activation_requires_review'
                    if args.diagnostic_runtime or args.manual_runtime else 'recovered',
                  customer_deployment_accepted=False,
                  native_server_performance_verified=False)
    directory = ROOT / "output/deployment-ubuntu-20261014/startup-readiness-trial8-20261017"
    destination = args.output or directory / ("ubuntu-public-identity-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
