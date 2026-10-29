"""Recheck the current trial bundle before handing it to an Ubuntu operator.

This is a read-only, dependency-free check.  It deliberately reports the
target-host gates instead of treating a Windows source checkout as a deployed
Linux service.  No database, account, secret, or network listener is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PACKAGE = ROOT / "output/deployment-ubuntu-20261014/permissions-trial45-20261022/metahub-2.0.0-trial.45-source.tar.gz"
DEFAULT_OUTPUT = ROOT / "output/deployment-ubuntu-20261014/predeploy-recheck-20261022.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    package = args.package.resolve()
    compose = (ROOT / "infra/production/compose.yml").read_text(encoding="utf-8")
    caddy = (ROOT / "infra/production/Caddyfile").read_text(encoding="utf-8")
    env = (ROOT / "infra/production/production.env.example").read_text(encoding="utf-8")
    active_env = "\n".join(line for line in env.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    manifest = read_json(package.parent / "source-integrity.json")
    static_audit = read_json(ROOT / "output/deployment-ubuntu-20261014/production-static-audit-20261022.json")
    route_smoke = read_json(ROOT / "output/deployment-ubuntu-20261014/ui-static-all-routes-20261022.json")
    actual_hash = sha256(package)

    checks = {
        "package_exists": package.is_file(),
        "package_hash_matches_integrity_record": actual_hash == manifest.get("sha256"),
        "release_is_trial45": manifest.get("release") == "2.0.0-trial.45",
        "postgres_is_not_host_published": "postgres:" in compose and "ports:" not in compose.split("  migrate:", 1)[0],
        "private_network_is_internal": "private: {internal: true}" in compose,
        "edge_defaults_to_loopback": 'METAHUB_BIND_IP:-127.0.0.1' in compose,
        "production_requires_postgresql": "METAHUB_DATABASE_URL=postgresql://" in env,
        "production_uses_verify_full": "sslmode=verify-full" in env,
        "development_bypasses_disabled": "METAHUB_DEV_PLATFORM_NO_MFA=false" in env and "METAHUB_PLATFORM_BOOTSTRAP_ENABLED=false" in env,
        "fixed_otp_absent": "000000" not in active_env and "test_capture" not in active_env,
        "caddy_hides_server_header": "-Server" in caddy,
        "no_local_dev_server_in_compose": "dev-server.mjs" not in compose,
    }
    open_ports = []
    for port in (5173, 8000, 8001, 8446):
        sock = socket.socket()
        sock.settimeout(0.05)
        try:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                open_ports.append(port)
        finally:
            sock.close()

    payload = {
        "status": "ready_for_target_host" if all(checks.values()) else "failed_local_recheck",
        "checked_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "release": manifest.get("release"),
        "package": str(package.relative_to(ROOT)),
        "package_bytes": package.stat().st_size if package.exists() else None,
        "package_sha256": actual_hash if package.exists() else None,
        "checks": checks,
        "local_open_development_ports": open_ports,
        "existing_static_audit": {
            "status": static_audit.get("status"),
            "failed_checks": static_audit.get("failed_checks", []),
            "operator_gates": static_audit.get("operator_gates", []),
        },
        "static_route_smoke": {
            "routes": route_smoke.get("routes"),
            "captures": route_smoke.get("captures"),
            "all_pages_verified": route_smoke.get("allPagesVerified"),
            "scope": route_smoke.get("scope"),
        },
        "target_host_gates": [
            "SSH access, Ubuntu architecture/resources and Docker must be checked on the target host.",
            "Real DNS, public TLS, firewall, SMTP and log-redaction samples must be checked on the target host.",
            "Trial.45 must be installed, migrated, backed up, restarted and restored on a PostgreSQL Linux copy before public binding.",
        ],
        "data_safety": "read-only source and package inspection; no database, account, secret, or public listener changed",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
