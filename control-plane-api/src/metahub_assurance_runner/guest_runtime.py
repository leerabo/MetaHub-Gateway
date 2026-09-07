"""Fixed SQLite/HTTPS workload inside an exclusively owned guest process.

This runs the complete Control app, not an authorization replacement. The profile
does not attest Docker isolation, PostgreSQL, external stores or fixture deletion.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import ipaddress
import logging
import os
import secrets
import shutil
import socket
import ssl
import tempfile
from contextlib import AsyncExitStack, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from metahub_shared.config_distribution import canonical_json_bytes
from pydantic import TypeAdapter

from .bootstrap import CreationObservation, HttpFixtureBootstrap
from .lease_channel import GuestLease, LeaseChannelError, LeaseTransport
from .privacy_queue import LocalPrivacyQueue
from .probes import SCENARIOS, FixedProbeRunner, ProbeResult
from .resources import LocalResourceInspector, ResourceObservation

_RESULTS = TypeAdapter(tuple[ProbeResult, ...])
_OBSERVATIONS = TypeAdapter(tuple[CreationObservation, ...])
_RESOURCES = TypeAdapter(tuple[ResourceObservation, ...])


def _configure(database: Path, origin: str) -> None:
    # Only called inside the disposable guest; never inherit developer/prod config.
    operating_system = {
        key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ
    }
    os.environ.clear()
    os.environ.update(operating_system)
    os.environ.update(
        {
            "METAHUB_ENV": "test",
            "METAHUB_DATABASE_URL": f"sqlite:///{database.as_posix()}",
            "METAHUB_ALLOWED_ORIGINS": origin,
            "METAHUB_LOG_LEVEL": "CRITICAL",
            "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE": "test_capture",
            "METAHUB_WORKSPACE_ALLOWED_REGIONS": "cn-north-1",
            "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED": "false",
            "METAHUB_FILE_STORAGE_ROOT": str(database.parent / "file-storage"),
        }
    )
    for name in (
        "AUTH_TOKEN",
        "IDENTITY_SESSION",
        "IDENTITY_CHALLENGE",
        "IDENTITY_PII_ENCRYPTION",
        "IDENTITY_MFA_ENCRYPTION",
        "TENANT_DATA_ENCRYPTION",
        "WORKSPACE_SESSION",
        "FILE_ENCRYPTION",
        "PLATFORM_SESSION",
    ):
        os.environ[f"METAHUB_{name}_SECRET"] = secrets.token_urlsafe(48)
    logging.disable(logging.CRITICAL)


def _certificate(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MetaHub isolated guest")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=5))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "server.crt", directory / "server.key"
    for target, content in (
        (cert_path, certificate.public_bytes(serialization.Encoding.PEM)),
        (
            key_path,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        ),
    ):
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
    return cert_path, key_path


async def _guard(lease: GuestLease) -> None:
    if not await lease.guard():
        raise LeaseChannelError("guest.lease_unavailable")


async def _startup(operation, lease: GuestLease):
    await _guard(lease)
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=1)
            await _guard(lease)
        return await task
    finally:
        # A cancelled thread still runs. Join it before removing its private files;
        # the supervisor owns the hard process/container deadline.
        await asyncio.shield(task)


def _initialize(baseline: Path, directory: Path, origin: str, migrations: Path):
    from metahub_shared.db.migrate import apply_migrations

    database = directory / "control.sqlite3"
    shutil.copyfile(baseline, database)
    _configure(database, origin)
    os.chdir(directory)
    apply_migrations(f"sqlite:///{database.as_posix()}", migrations_dir=migrations)
    cert_path, key_path = _certificate(directory)
    app = importlib.import_module("metahub_control.app").app
    if (
        app.dependency_overrides
        or app.state.config.database_url != f"sqlite:///{database.as_posix()}"
    ):
        raise ValueError("guest.application_boundary")
    return app, cert_path, key_path


async def execute_workload(
    lease: GuestLease, baseline: Path, scratch_parent: Path, migrations: Path
) -> bytes:
    """Python-owned inputs are fixed by the image entry point, never protocol fields."""
    started = datetime.now(UTC)
    for item in (baseline, scratch_parent, migrations):
        if not item.is_absolute() or item.is_symlink() or not item.exists():
            raise ValueError("guest.path_boundary")
    baseline_sha256 = hashlib.sha256(baseline.read_bytes()).hexdigest()
    original_directory = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="metahub-assurance-", dir=scratch_parent) as private:
        directory = Path(private)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server = None
        server_task = None
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            listener.setblocking(False)
            origin = f"https://127.0.0.1:{listener.getsockname()[1]}"
            app, cert_path, key_path = await _startup(
                lambda: _initialize(baseline, directory, origin, migrations),
                lease,
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=listener.getsockname()[1],
                    ssl_certfile=str(cert_path),
                    ssl_keyfile=str(key_path),
                    access_log=False,
                    proxy_headers=False,
                    log_config=None,
                    lifespan="on",
                    timeout_graceful_shutdown=2,
                )
            )
            server_task = asyncio.create_task(server.serve(sockets=[listener]))
            while not server.started:
                if server_task.done():
                    await server_task
                    raise RuntimeError("guest.server_unavailable")
                await _guard(lease)
                await asyncio.sleep(0.05)
            verification = ssl.create_default_context(cafile=str(cert_path))
            verification.minimum_version = ssl.TLSVersion.TLSv1_2

            async def mailbox(intent_id, email):
                from metahub_control.identity.delivery import pop_test_identity_challenge

                challenge = pop_test_identity_challenge("registration", intent_id)
                if (
                    challenge is None
                    or challenge.target != email
                    or challenge.expires_at <= datetime.now(UTC)
                ):
                    raise RuntimeError("guest.mailbox_unavailable")
                return challenge.code

            async with AsyncExitStack() as stack:
                clients = [
                    await stack.enter_async_context(
                        httpx.AsyncClient(
                            base_url=origin,
                            verify=verification,
                            trust_env=False,
                            follow_redirects=False,
                        )
                    )
                    for _ in range(4)
                ]
                bootstrap = HttpFixtureBootstrap(
                    *clients,
                    origin=origin,
                    environment_id=lease.binding.environment_id,
                    mailbox=mailbox,
                    lease_guard=lease.guard,
                    deadline_at=lease.binding.deadline_at,
                )
                fixtures = await bootstrap.run()
                resources = LocalResourceInspector(
                    directory / "control.sqlite3",
                    directory / "file-storage",
                    app.state.config.file_encryption_secret,
                    app.state.telemetry_runtime,
                    app.state.config.platform_session_secret,
                )
                runner = FixedProbeRunner(
                    fixtures,
                    lease.guard,
                    lease.binding.deadline_at,
                    resources=resources,
                    privacy_queue=LocalPrivacyQueue(app.state.config, resources),
                )
                results = []
                for code in SCENARIOS:
                    await _guard(lease)
                    results.append(await runner.run(code))
                await _guard(lease)
                return canonical_json_bytes(
                    {
                        "schema_version": "metahub.assurance-guest-report.v2",
                        "profile": "sqlite_https_v1",
                        "binding": lease.binding.model_dump(mode="json"),
                        "started_at": started.isoformat(),
                        "finished_at": datetime.now(UTC).isoformat(),
                        "baseline_sha256": baseline_sha256,
                        "runs": _RESULTS.dump_python(tuple(results), mode="json"),
                        "creations": _OBSERVATIONS.dump_python(
                            tuple(bootstrap.observations), mode="json"
                        ),
                        "resources": _RESOURCES.dump_python(
                            tuple(runner.resource_observations), mode="json"
                        ),
                        "isolation_attested": False,
                        "cleanup_attested": False,
                        "full_owner_coverage": False,
                    }
                )
        finally:
            if server is not None:
                server.should_exit = True
            try:
                if server_task is not None:
                    try:
                        await asyncio.wait_for(server_task, timeout=5)
                    finally:
                        if not server_task.done():
                            server_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await server_task
            finally:
                listener.close()
                try:
                    from metahub_shared.db.connection import (
                        _close_idle_database_connections,
                        close_database_pools,
                    )

                    _close_idle_database_connections()
                    close_database_pools()
                finally:
                    os.chdir(original_directory)


async def run_guest(
    transport: LeaseTransport, baseline: Path, scratch_parent: Path, migrations: Path
) -> int:
    lease = None
    try:
        lease = await GuestLease.accept(transport)
        remaining = (lease.binding.deadline_at - datetime.now(UTC)).total_seconds()
        async with asyncio.timeout(max(0, remaining)):
            await _guard(lease)
            report = await execute_workload(lease, baseline, scratch_parent, migrations)
            return 0 if await lease.publish(report) else 1
    except Exception:
        return 1
    finally:
        if lease is not None:
            lease.close()
        else:
            transport.close()
