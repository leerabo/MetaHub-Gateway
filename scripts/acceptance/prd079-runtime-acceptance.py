"""Bounded, repository-owned runtime acceptance for PRD-079.

The runner uses one empty SQLite database, real Control and Gateway processes,
and one loopback OpenAI-compatible provider. Published evidence contains only
allowlisted booleans, counts, fixed status values, and source SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import socket
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
for _source_root in (
    ROOT / "shared-sdk" / "python",
    ROOT / "gateway-data-plane" / "src",
    ROOT / "control-plane-api" / "src",
):
    sys.path.insert(0, str(_source_root))

from metahub_shared.db import (  # noqa: E402
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError  # noqa: E402
from metahub_shared.db.security import (  # noqa: E402
    encrypt_provider_key,
    hash_password,
    secret_prefix,
)


def _load_runtime_helpers() -> Any:
    """Load existing process/privacy helpers without starting resources."""

    name = "_metahub_prd075_runtime_helpers_for_prd079"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd075-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("runtime helper unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


_BASE = _load_runtime_helpers()
AcceptanceFailure = _BASE.AcceptanceFailure
SafeArgumentParser = _BASE.SafeArgumentParser
OwnedProcesses = _BASE.OwnedProcesses
ManagedProcess = _BASE.ManagedProcess
ProviderStub = _BASE.ProviderStub
require = _BASE.require
utc_now = _BASE.utc_now

LOOPBACK = "127.0.0.1"
GATEWAY_PORT = 18790
CONTROL_PORT = 18791
PROVIDER_PORT = 18792
FIXED_PORTS = (GATEWAY_PORT, CONTROL_PORT, PROVIDER_PORT)
TMP_ROOT = ROOT / ".tmp"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd079-runtime"
MIGRATIONS_DIR = ROOT / "infra" / "migrations"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
OWNER_FILENAME = ".prd079-runtime-owner"
EXPECTED_MIGRATION_COUNT = 72
EXPECTED_MIGRATION_VERSION = "0072"
QUOTA_TYPES = ("trial", "free", "gift", "paid")

SOURCE_FILES = {
    "runtime_acceptance": Path(__file__).resolve(),
    "runtime_helper": ROOT / "scripts" / "acceptance" / "prd075-runtime-acceptance.py",
    "migration_up": MIGRATIONS_DIR / "0072_plan_trial_quota_types.sql",
    "migration_down": MIGRATIONS_DIR / "0072_plan_trial_quota_types.down.sql",
    "plan_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "plan_billing"
    / "service.py",
    "plan_scheduler": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "plan_billing"
    / "scheduler.py",
    "quota_outbox": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "plan_billing"
    / "outbox.py",
    "quota_service": ROOT
    / "gateway-data-plane"
    / "src"
    / "metahub_gateway"
    / "quota"
    / "service.py",
    "gateway_api": ROOT
    / "gateway-data-plane"
    / "src"
    / "metahub_gateway"
    / "api"
    / "openai_compatible.py",
}

EVIDENCE_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "prd_id",
        "status",
        "generated_at",
        "all_108_prds_complete",
        "migration",
        "services",
        "control",
        "gateway",
        "ledger",
        "recovery",
        "database",
        "privacy",
        "cleanup",
        "source_provenance",
    }
)
EVIDENCE_SECTION_KEYS = {
    "services": frozenset(
        {"control_http", "gateway_http", "provider_loopback", "control_restarted"}
    ),
    "control": frozenset(
        {"plan_created", "quota_types_adjusted", "backend_rbac_denied", "request_id_contracts"}
    ),
    "gateway": frozenset(
        {
            "success_status",
            "exhausted_status",
            "provider_calls",
            "provider_not_called_after_exhaustion",
        }
    ),
    "ledger": frozenset(
        {
            "usage_rows",
            "finops_rows",
            "settlement_rows",
            "allocation_rows",
            "failed_request_tracked",
        }
    ),
    "recovery": frozenset(
        {"scheduler_recovered", "outbox_recovered", "trial_expired_events"}
    ),
    "database": frozenset({"integrity", "foreign_key_violations"}),
    "privacy": frozenset(
        {"files_scanned", "plaintext_matches", "sqlite_sidecars_remaining"}
    ),
    "cleanup": frozenset(
        {
            "owned_processes_stopped",
            "fixed_ports_released",
            "sqlite_sidecars_remaining",
            "work_removed",
        }
    ),
}
FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "token",
        "access_token",
        "authorization",
        "provider_key",
        "password",
        "secret",
        "prompt",
        "response",
        "content",
        "endpoint",
        "base_url",
        "database_path",
        "database_url",
        "encrypted_key",
        "ciphertext",
        "key_hash",
        "key_prefix",
        "idempotency_key",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    """Keep all runtime credentials and content canaries out of repr."""

    admin_password: str = field(repr=False)
    developer_password: str = field(repr=False)
    provider_key: str = field(repr=False)
    virtual_key: str = field(repr=False)
    auth_secret: str = field(repr=False)
    provider_secret: str = field(repr=False)
    prompt_secret: str = field(repr=False)
    file_secret: str = field(repr=False)
    batch_secret: str = field(repr=False)
    cache_secret: str = field(repr=False)
    approval_secret: str = field(repr=False)
    internal_token: str = field(repr=False)
    prompt_canary: str = field(repr=False)
    response_canary: str = field(repr=False)

    @classmethod
    def create(cls) -> RuntimeSecrets:
        def value(label: str) -> str:
            return f"Prd079-{label}-{secrets.token_urlsafe(48)}"

        return cls(
            admin_password=value("AdminPassword-Aa1"),
            developer_password=value("DeveloperPassword-Aa1"),
            provider_key=value("ProviderCredential"),
            virtual_key=f"mhv_prd079_{secrets.token_urlsafe(44)}",
            auth_secret=value("AuthHmac"),
            provider_secret=value("ProviderEncryption"),
            prompt_secret=value("PromptEncryption"),
            file_secret=value("FileEncryption"),
            batch_secret=value("BatchEncryption"),
            cache_secret=value("CacheHmac"),
            approval_secret=value("ApprovalHmac"),
            internal_token=value("InternalConfig"),
            prompt_canary=value("PrivatePrompt"),
            response_canary=value("PrivateResponse"),
        )

    def values(self, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        return (
            self.admin_password,
            self.developer_password,
            self.provider_key,
            self.virtual_key,
            self.auth_secret,
            self.provider_secret,
            self.prompt_secret,
            self.file_secret,
            self.batch_secret,
            self.cache_secret,
            self.approval_secret,
            self.internal_token,
            self.prompt_canary,
            self.response_canary,
            *extra,
        )


@dataclass(frozen=True, slots=True)
class RuntimeGraph:
    database: Path = field(repr=False)
    database_url: str = field(repr=False)
    tenant_id: str
    admin_email: str
    developer_email: str
    provider_id: str
    model_id: str
    model_alias: str
    migration: dict[str, dict[str, object]]


def safe_run_id(value: str | None) -> str:
    candidate = value or (
        datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ-") + secrets.token_hex(4)
    )
    compact = candidate.replace("-", "").replace("_", "")
    require(
        3 <= len(candidate) <= 80 and compact.isascii() and compact.isalnum(),
        "config.run_id",
        "invalid",
    )
    return candidate


@contextmanager
def temporary_environment(values: dict[str, str]) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for name, prior in previous.items():
            if prior is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prior


def _inside(path: Path, boundary: Path, *, stage: str) -> Path:
    try:
        resolved = path.resolve()
        root = boundary.resolve()
        resolved.relative_to(root)
        root.relative_to(ROOT.resolve())
    except (OSError, RuntimeError, ValueError):
        raise AcceptanceFailure(stage, "path") from None
    return resolved


def _valid_owner(owner: str) -> bool:
    return (
        len(owner) == 64
        and owner.isascii()
        and all(character in "0123456789abcdef" for character in owner)
    )


def create_run_directories(run_id: str, owner: str) -> tuple[Path, Path, Path]:
    require(_valid_owner(owner), "config.owner", "invalid")
    try:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        require(not TMP_ROOT.is_symlink(), "config.work_root", "symlink")
        require(not ARTIFACT_ROOT.is_symlink(), "config.artifact_root", "symlink")
        work = _inside(
            TMP_ROOT / f"prd079-runtime-{run_id}", TMP_ROOT, stage="config.work"
        )
        staging = _inside(
            ARTIFACT_ROOT / f".{run_id}.staging",
            ARTIFACT_ROOT,
            stage="config.artifact",
        )
        final = _inside(
            ARTIFACT_ROOT / run_id, ARTIFACT_ROOT, stage="config.artifact"
        )
        require(work.parent == TMP_ROOT.resolve(), "config.work", "parent")
        require(
            staging.parent == ARTIFACT_ROOT.resolve()
            and final.parent == ARTIFACT_ROOT.resolve(),
            "config.artifact",
            "parent",
        )
        require(not work.exists(), "config.work", "exists")
        require(not staging.exists() and not final.exists(), "config.artifact", "exists")
        work.mkdir()
        staging.mkdir()
        for name in ("files", "logs", "spool", "temp"):
            (work / name).mkdir()
        claim = owner + "\n"
        (work / OWNER_FILENAME).write_text(claim, encoding="ascii", newline="\n")
        (staging / OWNER_FILENAME).write_text(claim, encoding="ascii", newline="\n")
        return work, staging, final
    except AcceptanceFailure:
        raise
    except (OSError, RuntimeError):
        raise AcceptanceFailure("config.run_directory", "filesystem") from None


def _remove_owned_tree(path: Path, boundary: Path, owner: str, *, stage: str) -> None:
    target = _inside(path, boundary, stage=stage)
    require(target.parent == boundary.resolve(), stage, "parent")
    claim = target / OWNER_FILENAME
    try:
        require(target.is_dir() and not target.is_symlink(), stage, "claim")
        require(claim.is_file() and not claim.is_symlink(), stage, "claim")
        require(claim.read_text(encoding="ascii").strip() == owner, stage, "claim")
        shutil.rmtree(target)
        require(not target.exists(), stage, "remaining")
    except AcceptanceFailure:
        raise
    except (OSError, RuntimeError, UnicodeError):
        raise AcceptanceFailure(stage, "filesystem") from None


def remove_owned_work(work: Path, owner: str) -> None:
    _remove_owned_tree(work, TMP_ROOT, owner, stage="cleanup.work")


def remove_owned_staging(staging: Path, owner: str) -> None:
    if staging.exists():
        _remove_owned_tree(staging, ARTIFACT_ROOT, owner, stage="cleanup.staging")


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def migration_health(
    database_url: str,
    *,
    count: int,
    version: str,
    stage: str,
) -> dict[str, object]:
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count,MAX(version) AS version
            FROM schema_migrations WHERE status='applied'
            """
        ).fetchone()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        require(row is not None and int(row["count"]) == count, stage, "count")
        require(str(row["version"]) == version, stage, "version")
        require(integrity == "ok", stage, "integrity")
        require(foreign_keys == 0, stage, "foreign_keys")
        return {
            "count": count,
            "version": version,
            "integrity": integrity,
            "foreign_key_violations": foreign_keys,
        }
    finally:
        connection.close()


def prepare_database(
    work: Path,
    runtime: RuntimeSecrets,
    *,
    provider_url: str,
) -> RuntimeGraph:
    database = _inside(work / "prd079-runtime.sqlite3", work, stage="database.path")
    require(not database.exists(), "migration.fresh", "database_exists")
    database_url = _database_url(database)

    fresh = apply_migrations(database_url, migrations_dir=MIGRATIONS_DIR)
    require(
        len(fresh.applied) == EXPECTED_MIGRATION_COUNT
        and fresh.applied[-1:] == [EXPECTED_MIGRATION_VERSION],
        "migration.fresh",
        "result",
    )
    migration = {
        "fresh": migration_health(
            database_url,
            count=EXPECTED_MIGRATION_COUNT,
            version=EXPECTED_MIGRATION_VERSION,
            stage="migration.fresh",
        )
    }
    repeated = apply_migrations(database_url, migrations_dir=MIGRATIONS_DIR)
    require(
        repeated.applied == [] and len(repeated.skipped) == EXPECTED_MIGRATION_COUNT,
        "migration.repeat",
        "result",
    )
    migration["repeat"] = migration_health(
        database_url,
        count=EXPECTED_MIGRATION_COUNT,
        version=EXPECTED_MIGRATION_VERSION,
        stage="migration.repeat",
    )
    down = rollback_last_migration(
        database_url,
        migrations_dir=MIGRATIONS_DIR,
        expected_version=EXPECTED_MIGRATION_VERSION,
    )
    require(down.rolled_back == EXPECTED_MIGRATION_VERSION, "migration.down", "result")
    migration["down"] = migration_health(
        database_url,
        count=EXPECTED_MIGRATION_COUNT - 1,
        version="0071",
        stage="migration.down",
    )
    reup = apply_migrations(database_url, migrations_dir=MIGRATIONS_DIR)
    require(reup.applied == [EXPECTED_MIGRATION_VERSION], "migration.reup", "result")
    migration["reup"] = migration_health(
        database_url,
        count=EXPECTED_MIGRATION_COUNT,
        version=EXPECTED_MIGRATION_VERSION,
        stage="migration.reup",
    )

    seed_environment = {
        "METAHUB_SEED_ADMIN_PASSWORD": runtime.admin_password,
        "METAHUB_SEED_VIRTUAL_KEY": runtime.virtual_key,
        "METAHUB_SEED_PROVIDER_API_KEY": runtime.provider_key,
        "METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL": provider_url,
    }
    try:
        with temporary_environment(seed_environment):
            seed = apply_seed_data(database_url)
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        raise AcceptanceFailure("database.business_seed", "failed") from None

    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """
            SELECT provider.provider_id,model.model_id
            FROM providers provider
            JOIN models model ON model.tenant_id=provider.tenant_id
              AND model.provider_id=provider.provider_id
            WHERE provider.tenant_id=? AND provider.provider_code='local_openai'
              AND model.model_alias='default-chat'
            LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()
        require(owner is not None, "database.business_seed", "owner")
        provider_id = str(owner["provider_id"])
        model_id = str(owner["model_id"])
        now = utc_now()
        connection.execute(
            """
            UPDATE providers SET base_url=?,health_status='healthy',health_updated_at=?,updated_at=?
            WHERE tenant_id=? AND provider_id=?
            """,
            (provider_url, now, now, seed.tenant_id, provider_id),
        )
        connection.execute(
            """
            UPDATE models SET upstream_model_name='local-chat-model',
              provider_model_id='local-chat-model',input_price_per_1k=1,
              output_price_per_1k=1,currency='CNY',
              price_schema_version='prd079-runtime-v1',updated_at=?
            WHERE tenant_id=? AND model_id=?
            """,
            (now, seed.tenant_id, model_id),
        )
        encrypted = encrypt_provider_key(
            runtime.provider_key,
            encryption_secret=runtime.provider_secret,
        )
        fingerprint = hashlib.sha256(runtime.provider_key.encode("utf-8")).hexdigest()
        connection.execute(
            """
            UPDATE provider_credentials
            SET encrypted_key=?,key_fingerprint=?,name='runtime-provider',key_prefix=?,
              weight=100,probe_health_status='unknown',updated_at=?
            WHERE tenant_id=? AND provider_id=?
            """,
            (
                encrypted,
                fingerprint,
                secret_prefix(runtime.provider_key),
                now,
                seed.tenant_id,
                provider_id,
            ),
        )
        developer_email = "developer-prd079@local.invalid"
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES('user_prd079_runtime_developer',?,?,?,?,'developer','active',?,?)
            """,
            (
                seed.tenant_id,
                developer_email,
                "PRD-079 Runtime Developer",
                hash_password(runtime.developer_password),
                now,
                now,
            ),
        )
        connection.commit()
        require(
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            "database.business_seed",
            "integrity",
        )
        require(
            connection.execute("PRAGMA foreign_key_check").fetchall() == [],
            "database.business_seed",
            "foreign_keys",
        )
    except AcceptanceFailure:
        connection.rollback()
        raise
    except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        connection.rollback()
        raise AcceptanceFailure("database.business_seed", "failed") from None
    finally:
        connection.close()

    return RuntimeGraph(
        database=database,
        database_url=database_url,
        tenant_id=seed.tenant_id,
        admin_email=seed.admin_email,
        developer_email=developer_email,
        provider_id=provider_id,
        model_id=model_id,
        model_alias=seed.model_alias,
        migration=migration,
    )


def quiesce_database(graph: RuntimeGraph) -> None:
    connection = connect_database(graph.database_url)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        connection.execute("PRAGMA journal_mode=DELETE").fetchone()
    finally:
        connection.close()


def scan_private_storage(work: Path, private_values: tuple[str, ...]) -> dict[str, int]:
    return _BASE.verify_private_storage(work, private_values)


def service_environment(
    work: Path,
    graph: RuntimeGraph | Any,
    runtime: RuntimeSecrets,
) -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "OS",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
    }
    environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(
                str(item)
                for item in (
                    ROOT / "shared-sdk" / "python",
                    ROOT / "gateway-data-plane" / "src",
                    ROOT / "control-plane-api" / "src",
                )
            ),
            "PYTHONUNBUFFERED": "1",
            "TEMP": str(work / "temp"),
            "TMP": str(work / "temp"),
            "NO_PROXY": "127.0.0.1,localhost",
            "METAHUB_ENV": "dev",
            "METAHUB_LOG_LEVEL": "ERROR",
            "METAHUB_SERVICE_MODE": "internal_only",
            "METAHUB_DATABASE_URL": graph.database_url,
            "METAHUB_AUTH_TOKEN_SECRET": runtime.auth_secret,
            "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": runtime.provider_secret,
            "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET": runtime.prompt_secret,
            "METAHUB_FILE_ENCRYPTION_SECRET": runtime.file_secret,
            "METAHUB_BATCH_ENCRYPTION_SECRET": runtime.batch_secret,
            "METAHUB_CACHE_AFFINITY_HMAC_SECRET": runtime.cache_secret,
            "METAHUB_APPROVAL_HMAC_VERSION": "1",
            "METAHUB_APPROVAL_HMAC_SECRET": runtime.approval_secret,
            "METAHUB_INTERNAL_CONFIG_TOKEN": runtime.internal_token,
            "METAHUB_SESSION_RISK_ENABLED": "false",
            "METAHUB_GATEWAY_API_BASE_URL": f"http://{LOOPBACK}:{GATEWAY_PORT}",
            "METAHUB_CONTROL_API_BASE_URL": f"http://{LOOPBACK}:{CONTROL_PORT}",
            "METAHUB_ALLOWED_ORIGINS": f"http://{LOOPBACK}:{CONTROL_PORT}",
            "METAHUB_FINOPS_USAGE_SPOOL_ROOT": str(work / "spool"),
            "METAHUB_FILE_STORAGE_ROOT": str(work / "files"),
            "METAHUB_PROVIDER_HEALTH_ENABLED": "false",
            "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false",
            "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false",
            "METAHUB_USAGE_EXPORT_ENABLED": "false",
            "METAHUB_COST_ANOMALY_ENABLED": "false",
            "METAHUB_REQUEST_EXPLANATION_EVENT_ENABLED": "false",
            "METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED": "false",
            "METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED": "false",
            "METAHUB_BATCH_WORKER_ENABLED": "false",
            "METAHUB_RESPONSE_CACHE_ENABLED": "false",
            "METAHUB_APPROVAL_SCHEDULER_ENABLED": "false",
            "METAHUB_APPROVAL_OUTBOX_ENABLED": "false",
            "METAHUB_APPROVAL_WRITEBACK_ENABLED": "false",
        }
    )
    return environment


def _ports_available() -> bool:
    probes: list[socket.socket] = []
    try:
        for port in FIXED_PORTS:
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                candidate.bind((LOOPBACK, port))
            except OSError:
                candidate.close()
                return False
            probes.append(candidate)
        return True
    finally:
        for candidate in probes:
            candidate.close()


def assert_ports_available() -> None:
    require(_ports_available(), "config.ports", "fixed_port_unavailable")


def wait_ports_released() -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _ports_available():
            return
        time.sleep(0.1)
    raise AcceptanceFailure("cleanup.ports", "remaining")


def _request_headers(
    token: str,
    request_id: str,
    *,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    result = {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": request_id,
        "X-Trace-ID": f"trace-{request_id}",
    }
    if idempotency_key is not None:
        result["Idempotency-Key"] = idempotency_key
    return result


def _json_object(response: httpx.Response, *, stage: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise AcceptanceFailure(stage, "json") from None
    require(isinstance(payload, dict), stage, "shape")
    return payload


def _require_error(
    response: httpx.Response,
    *,
    status: int,
    code: str,
    request_id: str,
    stage: str,
) -> None:
    require(response.status_code == status, stage, "status")
    payload = _json_object(response, stage=stage)
    require(payload.get("code") == code, stage, "code")
    require(payload.get("request_id") == request_id, stage, "request_id")


def _login(
    client: httpx.Client,
    *,
    email: str,
    password: str,
    request_id: str,
) -> str:
    response = client.post(
        "/auth/login",
        headers={"X-Request-ID": request_id},
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    require(response.status_code == 200, "http.login", "status")
    payload = _json_object(response, stage="http.login")
    token = payload.get("access_token")
    require(isinstance(token, str) and len(token) > 40, "http.login", "token")
    return token


def _bucket_map(payload: dict[str, Any], *, stage: str) -> dict[str, dict[str, Any]]:
    buckets = payload.get("quota_buckets")
    require(isinstance(buckets, list) and len(buckets) == 4, stage, "buckets")
    result: dict[str, dict[str, Any]] = {}
    for item in buckets:
        require(isinstance(item, dict), stage, "bucket_shape")
        quota_type = item.get("quota_type")
        require(quota_type in QUOTA_TYPES and quota_type not in result, stage, "bucket_type")
        result[str(quota_type)] = item
    require(tuple(result) == QUOTA_TYPES, stage, "bucket_order")
    return result


def _create_plan(
    client: httpx.Client,
    token: str,
) -> tuple[dict[str, Any], int]:
    request_id = "prd079-runtime-plan-create"
    trial_end = (
        datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    ).isoformat().replace("+00:00", "Z")
    response = client.patch(
        "/plans",
        headers=_request_headers(token, request_id),
        json={
            "expected_revision": 0,
            "display_name": "PRD-079 Runtime Plan",
            "base_tier": "paid",
            "status": "active",
            "trial_ends_at": trial_end,
        },
    )
    require(response.status_code == 200, "http.plan", "status")
    payload = _json_object(response, stage="http.plan")
    require(payload.get("request_id") == request_id, "http.plan", "request_id")
    plan = payload.get("plan")
    require(
        isinstance(plan, dict)
        and plan.get("trial_status") == "active"
        and plan.get("trial_cycle") == 1,
        "http.plan",
        "trial",
    )
    _bucket_map(payload, stage="http.plan")
    return payload, 1


def _adjust_quota(
    client: httpx.Client,
    token: str,
    *,
    quota_type: str,
    direction: str,
    amount: str,
    revision: int,
    sequence: int,
) -> tuple[dict[str, Any], int]:
    request_id = f"prd079-runtime-adjust-{sequence:02d}"
    response = client.post(
        "/quota-adjustments",
        headers=_request_headers(
            token,
            request_id,
            idempotency_key=f"prd079-runtime-adjustment-{sequence:02d}",
        ),
        json={
            "quota_type": quota_type,
            "direction": direction,
            "amount": amount,
            "reason": "PRD-079 bounded runtime verification",
            "expected_bucket_revision": revision,
        },
    )
    require(response.status_code == 201, "http.adjustment", f"status_{sequence}")
    payload = _json_object(response, stage="http.adjustment")
    require(payload.get("request_id") == request_id, "http.adjustment", "request_id")
    bucket = payload.get("quota_bucket")
    require(
        isinstance(bucket, dict)
        and bucket.get("quota_type") == quota_type
        and isinstance(bucket.get("state_revision"), int),
        "http.adjustment",
        "bucket",
    )
    return payload, int(bucket["state_revision"])


def _read_quota_buckets(
    client: httpx.Client,
    token: str,
    *,
    request_id: str,
) -> dict[str, Any]:
    response = client.get(
        "/quota-buckets",
        headers=_request_headers(token, request_id),
    )
    require(response.status_code == 200, "http.quota_read", "status")
    payload = _json_object(response, stage="http.quota_read")
    require(payload.get("request_id") == request_id, "http.quota_read", "request_id")
    _bucket_map(payload, stage="http.quota_read")
    return payload


def _invoke_gateway(
    graph: RuntimeGraph,
    runtime: RuntimeSecrets,
    *,
    request_id: str,
    expected_status: int,
) -> dict[str, Any]:
    try:
        response = httpx.post(
            f"http://{LOOPBACK}:{GATEWAY_PORT}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {runtime.virtual_key}",
                "X-Request-ID": request_id,
                "X-Trace-ID": f"trace-{request_id}",
                "X-MetaHub-Contract-Tenant-ID": graph.tenant_id,
            },
            json={
                "model": graph.model_alias,
                "messages": [{"role": "user", "content": runtime.prompt_canary}],
                "max_tokens": 16,
                "stream": False,
            },
            timeout=20,
            follow_redirects=False,
            trust_env=False,
        )
    except httpx.HTTPError:
        raise AcceptanceFailure("http.gateway", "transport") from None
    require(response.status_code == expected_status, "http.gateway", "status")
    payload = _json_object(response, stage="http.gateway")
    if expected_status == 200:
        choices = payload.get("choices")
        require(isinstance(choices, list) and len(choices) == 1, "http.gateway", "choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        require(
            isinstance(message, dict) and message.get("content") == runtime.response_canary,
            "http.gateway",
            "answer",
        )
    else:
        require(payload.get("code") == "gateway.quota_exhausted", "http.gateway", "code")
        require(payload.get("request_id") == request_id, "http.gateway", "request_id")
    return payload


def _ledger_facts(graph: RuntimeGraph, request_id: str) -> dict[str, int | bool]:
    connection = connect_database(graph.database_url)
    try:
        usage_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM usage_ledger WHERE tenant_id=? AND request_id=?",
                (graph.tenant_id, request_id),
            ).fetchone()[0]
        )
        finops_rows = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM finops_ledger_entries
                WHERE tenant_id=? AND source_request_id=? AND entry_type='usage_charge'
                """,
                (graph.tenant_id, request_id),
            ).fetchone()[0]
        )
        settlement = connection.execute(
            """
            SELECT status,amount_micros,allocated_micros,unallocated_micros,
                   exposure_outstanding_micros
            FROM quota_usage_settlements WHERE tenant_id=? AND request_id=?
            """,
            (graph.tenant_id, request_id),
        ).fetchone()
        allocation_rows = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM quota_usage_allocations allocation
                JOIN quota_usage_settlements settlement
                  ON settlement.tenant_id=allocation.tenant_id
                 AND settlement.quota_usage_settlement_id=allocation.quota_usage_settlement_id
                WHERE settlement.tenant_id=? AND settlement.request_id=?
                """,
                (graph.tenant_id, request_id),
            ).fetchone()[0]
        )
        request_log = connection.execute(
            "SELECT status FROM request_logs WHERE tenant_id=? AND request_id=?",
            (graph.tenant_id, request_id),
        ).fetchone()
        require(usage_rows == 1, "database.ledger", "usage")
        require(finops_rows == 1, "database.ledger", "finops")
        require(settlement is not None, "database.ledger", "settlement")
        require(
            tuple(settlement)
            == ("settled", 12_000, 12_000, 0, 0),
            "database.ledger",
            "settlement_contract",
        )
        require(1 <= allocation_rows <= 4, "database.ledger", "allocations")
        require(
            request_log is not None and request_log[0] == "success",
            "database.ledger",
            "request",
        )
        return {
            "usage_rows": usage_rows,
            "finops_rows": finops_rows,
            "settlement_rows": 1,
            "allocation_rows": allocation_rows,
            "failed_request_tracked": False,
        }
    finally:
        connection.close()


def _exhausted_request_is_tracked(graph: RuntimeGraph, request_id: str) -> bool:
    connection = connect_database(graph.database_url)
    try:
        events = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM quota_events
                WHERE tenant_id=? AND event_type='quota.exhausted' AND source_request_id=?
                """,
                (graph.tenant_id, request_id),
            ).fetchone()[0]
        )
        failed = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM request_logs
                WHERE tenant_id=? AND request_id=? AND status='failed'
                """,
                (graph.tenant_id, request_id),
            ).fetchone()[0]
        )
        require(events == 1, "database.exhausted", "event")
        return events == 1 or failed == 1
    finally:
        connection.close()


def force_trial_due(graph: RuntimeGraph) -> None:
    connection = connect_database(graph.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = connection.execute(
            "SELECT * FROM plans WHERE tenant_id=? AND trial_status='active'",
            (graph.tenant_id,),
        ).fetchone()
        trial = connection.execute(
            "SELECT * FROM quota_buckets WHERE tenant_id=? AND quota_type='trial'",
            (graph.tenant_id,),
        ).fetchone()
        require(plan is not None and trial is not None, "recovery.force_due", "owner")
        updated_plan = connection.execute(
            """
            UPDATE plans SET trial_started_at='2020-01-01T00:00:00Z',
              trial_ends_at='2020-01-02T00:00:00Z',state_revision=state_revision+1,
              updated_at='2020-01-02T00:00:00Z'
            WHERE tenant_id=? AND plan_id=? AND state_revision=?
            """,
            (graph.tenant_id, plan["plan_id"], plan["state_revision"]),
        )
        updated_bucket = connection.execute(
            """
            UPDATE quota_buckets SET expires_at='2020-01-02T00:00:00Z',
              state_revision=state_revision+1,updated_at='2020-01-02T00:00:00Z'
            WHERE tenant_id=? AND quota_bucket_id=? AND state_revision=?
            """,
            (graph.tenant_id, trial["quota_bucket_id"], trial["state_revision"]),
        )
        require(
            updated_plan.rowcount == 1 and updated_bucket.rowcount == 1,
            "recovery.force_due",
            "cas",
        )
        connection.commit()
    except AcceptanceFailure:
        connection.rollback()
        raise
    except sqlite3.Error:
        connection.rollback()
        raise AcceptanceFailure("recovery.force_due", "database") from None
    finally:
        connection.close()


def _wait_for_recovery(
    graph: RuntimeGraph,
    client: httpx.Client,
    token: str,
) -> tuple[bool, bool, int, int]:
    deadline = time.monotonic() + 20
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        request_id = f"prd079-runtime-recovery-read-{attempts:02d}"
        response = client.get("/plans", headers=_request_headers(token, request_id))
        if response.status_code == 200:
            payload = _json_object(response, stage="recovery.http")
            require(payload.get("request_id") == request_id, "recovery.http", "request_id")
            plan = payload.get("plan")
            plan_expired = isinstance(plan, dict) and plan.get("trial_status") == "expired"
        else:
            plan_expired = False
        connection = connect_database(graph.database_url)
        try:
            row = connection.execute(
                """
                SELECT event.event_type,outbox.status
                FROM quota_events event
                JOIN quota_event_outbox outbox
                  ON outbox.tenant_id=event.tenant_id
                 AND outbox.quota_event_id=event.quota_event_id
                WHERE event.tenant_id=? AND event.event_type='trial.expired'
                """,
                (graph.tenant_id,),
            ).fetchone()
            event_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM quota_events
                    WHERE tenant_id=? AND event_type='trial.expired'
                    """,
                    (graph.tenant_id,),
                ).fetchone()[0]
            )
        finally:
            connection.close()
        outbox_done = row is not None and row["status"] == "dispatched"
        if plan_expired and outbox_done and event_count == 1:
            return True, True, event_count, attempts
        time.sleep(0.2)
    raise AcceptanceFailure("recovery.scheduler", "timeout")


def _exercise_http_runtime(
    work: Path,
    graph: RuntimeGraph,
    environment: dict[str, str],
    runtime: RuntimeSecrets,
    provider: ProviderStub,
    processes: OwnedProcesses,
) -> tuple[dict[str, object], tuple[str, ...]]:
    gateway = processes.add(
        _BASE.start_service(
            "gateway",
            "metahub_gateway.app:app",
            GATEWAY_PORT,
            work=work,
            environment=environment,
        )
    )
    _BASE.wait_health(
        f"http://{LOOPBACK}:{GATEWAY_PORT}/health",
        gateway,
        stage="health.gateway",
    )
    control = processes.add(
        _BASE.start_service(
            "control-initial",
            "metahub_control.app:app",
            CONTROL_PORT,
            work=work,
            environment=environment,
        )
    )
    _BASE.wait_health(
        f"http://{LOOPBACK}:{CONTROL_PORT}/health",
        control,
        stage="health.control",
    )

    request_id_contracts = 0
    with httpx.Client(
        base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
        timeout=httpx.Timeout(20, connect=3),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        admin_token = _login(
            client,
            email=graph.admin_email,
            password=runtime.admin_password,
            request_id="prd079-runtime-login-admin",
        )
        developer_token = _login(
            client,
            email=graph.developer_email,
            password=runtime.developer_password,
            request_id="prd079-runtime-login-developer",
        )
        denied_id = "prd079-runtime-rbac-denied"
        denied = client.get("/plans", headers=_request_headers(developer_token, denied_id))
        _require_error(
            denied,
            status=403,
            code="auth.permission_denied",
            request_id=denied_id,
            stage="http.rbac",
        )
        request_id_contracts += 1

        created, contracts = _create_plan(client, admin_token)
        request_id_contracts += contracts
        buckets = _bucket_map(created, stage="http.plan")
        revisions = {
            quota_type: int(buckets[quota_type]["state_revision"])
            for quota_type in QUOTA_TYPES
        }
        sequence = 0
        for quota_type in QUOTA_TYPES:
            sequence += 1
            _payload, revisions[quota_type] = _adjust_quota(
                client,
                admin_token,
                quota_type=quota_type,
                direction="credit",
                amount="1.000000",
                revision=revisions[quota_type],
                sequence=sequence,
            )
            request_id_contracts += 1

        provider_before = int(provider.snapshot()["calls"])
        require(provider_before == 0, "provider.success", "preexisting_call")
        success_id = "prd079-runtime-gateway-success"
        _invoke_gateway(graph, runtime, request_id=success_id, expected_status=200)
        provider_after_success = int(provider.snapshot()["calls"])
        require(provider_after_success == 1, "provider.success", "call_count")
        ledger = _ledger_facts(graph, success_id)

        quota_payload = _read_quota_buckets(
            client,
            admin_token,
            request_id="prd079-runtime-quota-before-drain",
        )
        request_id_contracts += 1
        current = _bucket_map(quota_payload, stage="http.quota_read")
        for quota_type in QUOTA_TYPES:
            amount = str(current[quota_type]["available"])
            require(amount.endswith("0000") or "." in amount, "http.quota_read", "amount")
            if amount == "0.000000":
                continue
            sequence += 1
            _payload, revisions[quota_type] = _adjust_quota(
                client,
                admin_token,
                quota_type=quota_type,
                direction="debit",
                amount=amount,
                revision=int(current[quota_type]["state_revision"]),
                sequence=sequence,
            )
            request_id_contracts += 1

        drained = _read_quota_buckets(
            client,
            admin_token,
            request_id="prd079-runtime-quota-drained",
        )
        request_id_contracts += 1
        require(
            all(
                bucket["available"] == "0.000000"
                for bucket in _bucket_map(drained, stage="http.quota_read").values()
            ),
            "http.quota_read",
            "not_drained",
        )

        exhausted_id = "prd079-runtime-gateway-exhausted"
        _invoke_gateway(graph, runtime, request_id=exhausted_id, expected_status=402)
        request_id_contracts += 1
        provider_after_exhaustion = int(provider.snapshot()["calls"])
        require(
            provider_after_exhaustion == provider_after_success,
            "provider.exhausted",
            "unexpected_call",
        )
        ledger["failed_request_tracked"] = _exhausted_request_is_tracked(
            graph, exhausted_id
        )

    processes.stop(control)
    force_trial_due(graph)
    recovered_control = processes.add(
        _BASE.start_service(
            "control-recovery",
            "metahub_control.app:app",
            CONTROL_PORT,
            work=work,
            environment=environment,
        )
    )
    _BASE.wait_health(
        f"http://{LOOPBACK}:{CONTROL_PORT}/health",
        recovered_control,
        stage="health.control_recovery",
    )
    with httpx.Client(
        base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
        timeout=httpx.Timeout(20, connect=3),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        recovery_token = _login(
            client,
            email=graph.admin_email,
            password=runtime.admin_password,
            request_id="prd079-runtime-login-recovery",
        )
        scheduler_recovered, outbox_recovered, trial_events, recovery_requests = (
            _wait_for_recovery(graph, client, recovery_token)
        )
        request_id_contracts += recovery_requests

    provider_state = provider.snapshot()
    require(
        provider_state == {
            "calls": 1,
            "failures": 0,
            "invalid": 0,
            "auth_mismatches": 0,
        },
        "provider.final",
        "counters",
    )
    return (
        {
            "services": {
                "control_http": True,
                "gateway_http": True,
                "provider_loopback": True,
                "control_restarted": True,
            },
            "control": {
                "plan_created": True,
                "quota_types_adjusted": 4,
                "backend_rbac_denied": True,
                "request_id_contracts": request_id_contracts,
            },
            "gateway": {
                "success_status": 200,
                "exhausted_status": 402,
                "provider_calls": int(provider_state["calls"]),
                "provider_not_called_after_exhaustion": True,
            },
            "ledger": ledger,
            "recovery": {
                "scheduler_recovered": scheduler_recovered,
                "outbox_recovered": outbox_recovered,
                "trial_expired_events": trial_events,
            },
        },
        (admin_token, developer_token, recovery_token),
    )


def _close_runtime_owners(processes: OwnedProcesses, provider: ProviderStub) -> None:
    first_failure: AcceptanceFailure | None = None
    try:
        processes.close()
    except Exception:
        first_failure = AcceptanceFailure("cleanup.owners", "process")
    try:
        provider.stop()
    except Exception:
        if first_failure is None:
            first_failure = AcceptanceFailure("cleanup.owners", "provider")
    if first_failure is not None:
        raise first_failure


def execute_runtime(
    work: Path,
    graph: RuntimeGraph,
    runtime: RuntimeSecrets,
) -> tuple[dict[str, object], tuple[str, ...]]:
    environment = service_environment(work, graph, runtime)
    provider = ProviderStub(
        port=PROVIDER_PORT,
        model="local-chat-model",
        key=runtime.provider_key,
        answer=runtime.response_canary,
        label="prd079",
    )
    processes = OwnedProcesses()
    try:
        provider.start()
        result = _exercise_http_runtime(
            work,
            graph,
            environment,
            runtime,
            provider,
            processes,
        )
    except BaseException:
        try:
            _close_runtime_owners(processes, provider)
        except AcceptanceFailure:
            pass
        raise
    _close_runtime_owners(processes, provider)
    wait_ports_released()
    return result


def final_database_health(graph: RuntimeGraph) -> dict[str, object]:
    connection = connect_database(graph.database_url)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        pending_reservations = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM quota_reservations
                WHERE tenant_id=? AND status IN('active','exposure_pending')
                """,
                (graph.tenant_id,),
            ).fetchone()[0]
        )
        reserved_micros = int(
            connection.execute(
                "SELECT COALESCE(SUM(reserved_micros),0) FROM quota_buckets WHERE tenant_id=?",
                (graph.tenant_id,),
            ).fetchone()[0]
        )
        pending_outbox = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM quota_event_outbox
                WHERE tenant_id=? AND status IN('pending','leased')
                """,
                (graph.tenant_id,),
            ).fetchone()[0]
        )
        plan = connection.execute(
            "SELECT trial_status FROM plans WHERE tenant_id=?",
            (graph.tenant_id,),
        ).fetchone()
        require(integrity == "ok", "database.final", "integrity")
        require(foreign_keys == 0, "database.final", "foreign_keys")
        require(pending_reservations == 0, "database.final", "reservations")
        require(reserved_micros == 0, "database.final", "reserved")
        require(pending_outbox == 0, "database.final", "outbox")
        require(plan is not None and plan[0] == "expired", "database.final", "trial")
        return {"integrity": integrity, "foreign_key_violations": foreign_keys}
    finally:
        connection.close()


def source_provenance() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in SOURCE_FILES.items():
        try:
            require(path.is_file() and not path.is_symlink(), "evidence.source", "missing")
            path.resolve().relative_to(ROOT.resolve())
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except AcceptanceFailure:
            raise
        except (OSError, RuntimeError, ValueError):
            raise AcceptanceFailure("evidence.source", "filesystem") from None
    return result


def assert_safe_evidence(value: object, private_values: tuple[str, ...]) -> None:
    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).casefold() in FORBIDDEN_EVIDENCE_KEYS:
                    raise AcceptanceFailure("evidence.shape", "forbidden_key")
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raise AcceptanceFailure("evidence.shape", "not_json") from None
    if any(private and private in serialized for private in private_values):
        raise AcceptanceFailure("evidence.privacy", "private_value")


def _validate_evidence(payload: dict[str, object]) -> None:
    require(set(payload) == EVIDENCE_TOP_LEVEL_KEYS, "evidence.shape", "top_level")
    require(payload.get("schema_version") == 1, "evidence.shape", "schema")
    require(payload.get("prd_id") == "PRD-079", "evidence.shape", "prd")
    require(payload.get("status") == "passed", "evidence.shape", "status")
    require(
        payload.get("all_108_prds_complete") is False,
        "evidence.shape",
        "global_status",
    )
    migration = payload.get("migration")
    require(
        isinstance(migration, dict)
        and set(migration) == {"fresh", "repeat", "down", "reup"},
        "evidence.shape",
        "migration",
    )
    for name, expected_count, expected_version in (
        ("fresh", 72, "0072"),
        ("repeat", 72, "0072"),
        ("down", 71, "0071"),
        ("reup", 72, "0072"),
    ):
        stage = migration.get(name)
        require(
            isinstance(stage, dict)
            and set(stage)
            == {"count", "version", "integrity", "foreign_key_violations"}
            and stage.get("count") == expected_count
            and stage.get("version") == expected_version
            and stage.get("integrity") == "ok"
            and stage.get("foreign_key_violations") == 0,
            "evidence.shape",
            f"migration_{name}",
        )
    for name, expected_keys in EVIDENCE_SECTION_KEYS.items():
        section = payload.get(name)
        require(
            isinstance(section, dict) and set(section) == expected_keys,
            "evidence.shape",
            name,
        )
    provenance = payload.get("source_provenance")
    require(
        isinstance(provenance, dict) and set(provenance) == set(SOURCE_FILES),
        "evidence.shape",
        "source_provenance",
    )
    require(
        all(
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            for digest in provenance.values()
        ),
        "evidence.shape",
        "source_hash",
    )


def empty_evidence_payload() -> dict[str, object]:
    migration = {
        "fresh": {
            "count": 72,
            "version": "0072",
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
        "repeat": {
            "count": 72,
            "version": "0072",
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
        "down": {
            "count": 71,
            "version": "0071",
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
        "reup": {
            "count": 72,
            "version": "0072",
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
    }
    return {
        "schema_version": 1,
        "prd_id": "PRD-079",
        "status": "passed",
        "generated_at": utc_now(),
        "all_108_prds_complete": False,
        "migration": migration,
        "services": {
            "control_http": True,
            "gateway_http": True,
            "provider_loopback": True,
            "control_restarted": True,
        },
        "control": {
            "plan_created": True,
            "quota_types_adjusted": 4,
            "backend_rbac_denied": True,
            "request_id_contracts": 1,
        },
        "gateway": {
            "success_status": 200,
            "exhausted_status": 402,
            "provider_calls": 1,
            "provider_not_called_after_exhaustion": True,
        },
        "ledger": {
            "usage_rows": 1,
            "finops_rows": 1,
            "settlement_rows": 1,
            "allocation_rows": 1,
            "failed_request_tracked": True,
        },
        "recovery": {
            "scheduler_recovered": True,
            "outbox_recovered": True,
            "trial_expired_events": 1,
        },
        "database": {"integrity": "ok", "foreign_key_violations": 0},
        "privacy": {
            "files_scanned": 0,
            "plaintext_matches": 0,
            "sqlite_sidecars_remaining": 0,
        },
        "cleanup": {
            "owned_processes_stopped": True,
            "fixed_ports_released": True,
            "sqlite_sidecars_remaining": 0,
            "work_removed": True,
        },
        "source_provenance": source_provenance(),
    }


def write_evidence(
    staging: Path,
    final: Path,
    payload: dict[str, object],
    private_values: tuple[str, ...],
    owner: str | None = None,
) -> Path:
    _validate_evidence(payload)
    assert_safe_evidence(payload, private_values)
    require(staging.is_dir() and not staging.is_symlink(), "evidence.path", "staging")
    require(not final.exists(), "evidence.path", "final_exists")
    require(staging.parent == final.parent, "evidence.path", "parent")
    if owner is not None:
        claim = staging / OWNER_FILENAME
        require(_valid_owner(owner), "evidence.path", "owner")
        require(
            claim.is_file()
            and not claim.is_symlink()
            and claim.read_text(encoding="ascii").strip() == owner,
            "evidence.path",
            "claim",
        )
        claim.unlink()
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        evidence_path = staging / "runtime-acceptance.json"
        manifest_path = staging / "SHA256SUMS.txt"
        with evidence_path.open("xb") as stream:
            stream.write(encoded)
        persisted = evidence_path.read_bytes()
        require(persisted == encoded, "evidence.publish", "json_mismatch")
        manifest = f"{hashlib.sha256(persisted).hexdigest()}  runtime-acceptance.json\n"
        manifest_path.write_text(manifest, encoding="ascii", newline="\n")
        require(
            manifest_path.read_text(encoding="ascii") == manifest,
            "evidence.publish",
            "manifest_mismatch",
        )
        scan_private_storage(staging, private_values)
        staging.replace(final)
    except AcceptanceFailure:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise AcceptanceFailure("evidence.publish", "filesystem") from None
    return final / "runtime-acceptance.json"


def _cleanup_failed_run(
    work: Path,
    staging: Path,
    owner: str,
    graph: RuntimeGraph | None,
) -> None:
    if graph is not None:
        try:
            quiesce_database(graph)
        except Exception:
            pass
    try:
        wait_ports_released()
    except AcceptanceFailure:
        pass
    try:
        if work.exists():
            remove_owned_work(work, owner)
    except AcceptanceFailure:
        pass
    try:
        remove_owned_staging(staging, owner)
    except AcceptanceFailure:
        pass


def run(run_id: str | None = None) -> Path:
    actual_run_id = safe_run_id(run_id)
    owner = hashlib.sha256(secrets.token_bytes(48)).hexdigest()
    runtime = RuntimeSecrets.create()
    before = source_provenance()
    assert_ports_available()
    work, staging, final = create_run_directories(actual_run_id, owner)
    graph: RuntimeGraph | None = None
    try:
        graph = prepare_database(
            work,
            runtime,
            provider_url=f"http://{LOOPBACK}:{PROVIDER_PORT}/v1",
        )
        facts, runtime_private = execute_runtime(work, graph, runtime)
        database = final_database_health(graph)
        quiesce_database(graph)
        # The owner claim is intentionally present until the exact work tree is
        # removed. It is a cleanup capability, not business evidence content.
        private_values = runtime.values(runtime_private)
        privacy = scan_private_storage(work, private_values)
        require(source_provenance() == before, "evidence.source", "changed")
        wait_ports_released()
        remove_owned_work(work, owner)

        payload = empty_evidence_payload()
        payload["generated_at"] = utc_now()
        payload["migration"] = graph.migration
        payload["services"] = facts["services"]
        payload["control"] = facts["control"]
        payload["gateway"] = facts["gateway"]
        payload["ledger"] = facts["ledger"]
        payload["recovery"] = facts["recovery"]
        payload["database"] = database
        payload["privacy"] = privacy
        payload["cleanup"] = {
            "owned_processes_stopped": True,
            "fixed_ports_released": True,
            "sqlite_sidecars_remaining": int(privacy["sqlite_sidecars_remaining"]),
            "work_removed": True,
        }
        payload["source_provenance"] = before
        evidence = write_evidence(staging, final, payload, private_values, owner)
        scan_private_storage(final, private_values)
        return evidence
    except BaseException:
        _cleanup_failed_run(work, staging, owner, graph)
        raise


def parse_arguments(argv: list[str] | None = None) -> Any:
    parser = SafeArgumentParser(description="Run PRD-079 runtime acceptance")
    parser.add_argument("--run-id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parse_arguments(argv)
        evidence = run(arguments.run_id)
    except AcceptanceFailure as exc:
        print(
            f"PRD-079 runtime acceptance failed: {exc.stage}:{exc.code}",
            file=sys.stderr,
        )
        return 1
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, RuntimeError, TypeError):
        print(
            "PRD-079 runtime acceptance failed: runtime:dependency_failure",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "PRD-079 runtime acceptance failed: runtime:unexpected_failure",
            file=sys.stderr,
        )
        return 1
    print(evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
