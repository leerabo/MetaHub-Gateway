"""Create a fresh isolated Ubuntu deployment configuration; never overwrite secrets.

For an existing database migration, supply its original encryption roots separately
to the audited importer. Generating new roots does not decrypt existing records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def deployment_identity(root: Path) -> tuple[str, str]:
    """Use the verified release bundle's identity, never an implicit old version."""
    manifest = root / "release-manifest.json"
    if not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Prepare production from a verified release bundle with its manifest.")
    contents = manifest.read_bytes()
    value = json.loads(contents)
    if (not isinstance(value, dict)
            or not isinstance(value.get("release"), str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?", value["release"])
            or not isinstance(value.get("files"), list) or not value["files"]):
        raise ValueError("Release manifest has no valid deployment identity.")
    return value["release"], "release-manifest-sha256:" + hashlib.sha256(contents).hexdigest()


def write(path: Path, contents: str, mode: int = 0o600) -> None:
    path.write_text(contents, encoding="utf-8", newline="\n")
    path.chmod(mode)


def runtime_template_for_release(template: str, release: str) -> str:
    """Bind the runtime's reported version to the verified release manifest."""
    lines = template.splitlines()
    replaced = False
    result: list[str] = []
    for line in lines:
        if line.startswith("METAHUB_VERSION="):
            result.append(f"METAHUB_VERSION={release}")
            replaced = True
        else:
            result.append(line)
    if not replaced:
        result.append(f"METAHUB_VERSION={release}")
    return "\n".join(result).rstrip("\n") + "\n"


def openssl(*args: str) -> None:
    result = subprocess.run(["openssl", *args], capture_output=True)
    if result.returncode:
        raise RuntimeError("OpenSSL certificate generation failed; no secrets were printed.")


def certificate(state: Path, directory: Path, name: str, san: str) -> None:
    ca = state / "authority"
    key = directory / f"{name}.key"
    request = ca / f"{name}.csr"
    config = ca / f"{name}.ext"
    write(config, f"subjectAltName={san}\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
    openssl("genrsa", "-out", str(key), "2048")
    openssl("req", "-new", "-key", str(key), "-out", str(request), "-subj", f"/CN={name}")
    openssl("x509", "-req", "-in", str(request), "-CA", str(ca / "ca.crt"), "-CAkey", str(ca / "ca.key"), "-set_serial", str(secrets.randbits(128)), "-days", "365", "-sha256", "-extfile", str(config), "-out", str(directory / f"{name}.crt"))
    key.chmod(0o400)
    (directory / f"{name}.crt").chmod(0o444)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--local-validation-tls", action="store_true")
    args = parser.parse_args()
    if os.name == "posix" and os.geteuid() != 0:
        raise ValueError("Run as root on Ubuntu to set container secret ownership.")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", args.domain) or "." not in args.domain:
        raise ValueError("Supply a DNS domain without scheme, port or path.")
    state = args.state.resolve()
    if state.exists():
        raise ValueError("State path already exists; refusing to replace encryption roots or data.")
    release, build_commit = deployment_identity(ROOT)
    if not shutil.which("openssl"):
        raise ValueError("Install OpenSSL before preparing the deployment.")
    old_umask = os.umask(0o077)
    try:
        for name in ("authority", "backend/tls", "postgres/tls", "edge", "caddy-data", "caddy-config", "backups", "updates/requests", "updates/status"):
            (state / name).mkdir(parents=True, exist_ok=False)
        # Root traversal is required for the distinct, unprivileged container users.
        state.chmod(0o711)
        for name in ("backend", "postgres", "backend/tls", "postgres/tls", "edge"):
            (state / name).chmod(0o755)
        ca = state / "authority"
        openssl("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-keyout", str(ca / "ca.key"), "-out", str(ca / "ca.crt"), "-days", "1825", "-sha256", "-subj", "/CN=MetaHub private service authority", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        certificate(state, state / "backend/tls", "control", "DNS:control-api,DNS:localhost,IP:127.0.0.1")
        certificate(state, state / "backend/tls", "gateway", "DNS:gateway,DNS:localhost,IP:127.0.0.1")
        certificate(state, state / "postgres/tls", "postgres", "DNS:postgres")
        for directory in (state / "backend/tls", state / "postgres/tls"):
            shutil.copyfile(ca / "ca.crt", directory / "ca.crt")
            (directory / "ca.crt").chmod(0o444)
        if args.local_validation_tls:
            certificate(state, state / "edge", "public", f"DNS:{args.domain},DNS:localhost,IP:127.0.0.1")
        app_password, admin_password = secrets.token_hex(32), secrets.token_hex(32)
        write(state / "postgres/app-password", app_password + "\n", 0o400)
        write(state / "postgres/admin-password", admin_password + "\n", 0o400)
        write(state / "backups/encryption.key", secrets.token_hex(32) + "\n", 0o400)
        write(state / "updates/catalog.json", '{"releases":[]}\n', 0o444)
        for name in ("updates", "updates/status"):
            (state / name).chmod(0o755)
        template = runtime_template_for_release(
            (ROOT / "infra/production/production.env.example").read_text(), release
        )
        template = template.replace("trial.example.com", args.domain).replace("REPLACE_PASSWORD", app_password)
        # Generate independent roots, including disabled feature domains. Never reuse
        # a service authentication token as an encryption or signature root.
        # Avoid importing runtime dependencies just to enumerate the required names.
        import ast
        module = ast.parse((ROOT / "infra/production/service.py").read_text())
        roots = next(ast.literal_eval(node.value) for node in module.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SECRET_ROOTS" for t in node.targets))
        names = {"METAHUB_" + name + "_SECRET" for name in roots}
        names.update({"METAHUB_INTERNAL_CONFIG_TOKEN", "METAHUB_INTERNAL_CHAT_TOKEN", "METAHUB_INTERNAL_BATCH_TOKEN", "METAHUB_BATCH_ENCRYPTION_SECRET", "METAHUB_BUSINESS_VERIFICATION_ENCRYPTION_SECRET", "METAHUB_PAYMENT_WEBHOOK_SECRET", "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_SECRET", "METAHUB_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET"})
        template += "\n" + "\n".join(f"{name}={secrets.token_hex(32)}" for name in sorted(names)) + "\n"
        write(state / "backend/runtime.env", template, 0o400)
        tls = "tls /run/tls/public.crt /run/tls/public.key" if args.local_validation_tls else ""
        write(state / "compose.env", f"METAHUB_RELEASE={release}\nMETAHUB_BUILD_COMMIT={build_commit}\nMETAHUB_STATE_DIR={state.as_posix()}\nMETAHUB_DOMAIN={args.domain}\nMETAHUB_BIND_IP=127.0.0.1\nMETAHUB_TLS_POLICY={tls}\n")
        if os.name == "posix":
            os.chown(state / "updates/requests", 10001, 10001)
            for path in (state / "backend").rglob("*"):
                os.chown(path, 10001, 10001)
            for path in (state / "postgres").rglob("*"):
                os.chown(path, 999, 999)
            for directory in ("edge", "caddy-data", "caddy-config"):
                for path in (state / directory, *(state / directory).rglob("*")):
                    os.chown(path, 10001, 10001)
        print(json.dumps({"status": "prepared", "state": str(state), "domain": args.domain, "publicly_bound": False, "local_validation_tls": args.local_validation_tls}))
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    main()
