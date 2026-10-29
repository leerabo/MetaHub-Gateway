"""Verify the isolated Ubuntu deployment through real CA-verified HTTPS.

Only the public fixture CA is fetched over pinned SSH. No credential or cookie
is recorded, and no public listener or DNS record is configured.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import ssl
import time

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_STATES = ("/opt/metahub-trial-state", "/opt/metahub-trial-cold-recovery-trial8-20261017")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', help='Require this exact installed runtime version')
    parser.add_argument('--state', choices=FIXTURE_STATES, default=FIXTURE_STATES[0])
    parser.add_argument('--output', type=Path, default=ROOT / 'output/deployment-ubuntu-20261014/https-ubuntu-validation.json')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location(
        "ubuntu_ssh", ROOT / "scripts/acceptance/trial-ubuntu-ssh.py")
    ssh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ssh)
    client = ssh.connect()
    try:
        _, out, _ = client.exec_command(
            "test -f /home/acceptance/data-verification-install-passed && "
            f"sudo cat {args.state}/authority/ca.crt", timeout=60)
        ca = out.read().decode("ascii")
        if out.channel.recv_exit_status() != 0 or not ca.startswith("-----BEGIN CERTIFICATE-----"):
            raise ValueError("Installed fixture public CA is unavailable")
        _, details, _ = client.exec_command(
            "sudo docker inspect --format '{{json .Mounts}}' metahub-trial-control-api-1", timeout=60,
        )
        mounts = json.loads(details.read())
        if details.channel.recv_exit_status() != 0:
            raise ValueError("Cannot establish tested container mount scope")
        if not any(item.get('Destination') == '/run/secrets/runtime.env'
                   and item.get('Source') == args.state + '/backend/runtime.env' for item in mounts):
            raise ValueError("Serving control container does not use the selected fixture state")
        diagnostic_mounts = [item["Destination"] for item in mounts
                             if item.get("Source", "").startswith("/home/acceptance/")]
        code_mounts = [item['Destination'] for item in mounts
                       if item['Destination'] == '/app'
                       or (item['Destination'].startswith('/app/') and item['Destination'] != '/app/.data')]
    finally:
        client.close()
    context = ssl.create_default_context(cadata=ca)
    observations = []
    started = time.monotonic()
    checks = [
        ("/", 200), ("/identity.html", 200), ("/console.html", 200),
        ("/platform.html", 200), ("/health/ready", 200), ("/version", 200),
        ("/internal/health", 404), ("/docs", 404), ("/openapi.json", 404),
        ("/.env", 404), ("/package.json", 404),
    ]
    version = None
    for path, expected in checks:
        raw = socket.create_connection(("127.0.0.1", 8446), timeout=30)
        connection = http.client.HTTPSConnection("trial.local", 8446, context=context, timeout=30)
        connection.sock = context.wrap_socket(raw, server_hostname="trial.local")
        try:
            connection.request("GET", path, headers={"Host": "trial.local:8446", "Connection": "close"})
            response = connection.getresponse()
            body = response.read(2 * 1024 * 1024)
            observation = dict(path=path, expected=expected, actual=response.status,
                               bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
            if response.status != expected:
                raise AssertionError(f"Unexpected HTTP status for {path}: {response.status}")
            for name, expected_value in (
                ("Strict-Transport-Security", "max-age=31536000"),
                ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
            ):
                if response.getheader(name) != expected_value:
                    raise AssertionError(f"Missing security header {name} for {path}")
            if path == "/version":
                value = json.loads(body)
                version = {key: value[key] for key in ("version", "build_commit") if key in value}
                if args.release and version.get('version') != args.release:
                    raise AssertionError('The serving runtime does not match the requested release')
            observations.append(observation)
        finally:
            connection.close()
    report = dict(status="passed", platform="Ubuntu Docker through private HTTPS ingress",
                  certificate_chain_and_hostname_verified=True, ignored_tls_errors=False,
                  public_domain_or_listener_enabled=False, credential_requests=False,
                  diagnostic_code_overrides=diagnostic_mounts,
                  application_code_mounts=code_mounts,
                  application_code_overrides_absent=not diagnostic_mounts and not code_mounts,
                  image_source_integrity_verified_by_this_probe=False,
                  expected_release=args.release,
                  fixture_state=args.state,
                  version=version, checks=observations,
                  elapsed_seconds=round(time.monotonic() - started, 3))
    if args.release and (diagnostic_mounts or code_mounts):
        raise AssertionError('The serving container has application code overrides')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
