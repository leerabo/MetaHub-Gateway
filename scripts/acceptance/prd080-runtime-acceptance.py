"""Bounded, repository-owned runtime acceptance for PRD-080.

The runner migrates one empty SQLite database, starts real Gateway and Control
processes plus loopback Provider and Webhook receivers, and publishes only
allowlisted booleans, counts, migration versions, and SHA-256 provenance.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from metahub_shared.rbac import permission_matrix_sha256  # noqa: E402
from metahub_shared.runtime.webhooks import verify_webhook_signature  # noqa: E402


def _load_runtime_helpers() -> Any:
    """Load the established PRD-079 process and privacy helpers without side effects."""

    name = "_metahub_prd079_runtime_helpers_for_prd080"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd079-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("runtime helper unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


_BASE = _load_runtime_helpers()
_PROCESS = _BASE._BASE
AcceptanceFailure = _BASE.AcceptanceFailure
SafeArgumentParser = _BASE.SafeArgumentParser
OwnedProcesses = _BASE.OwnedProcesses
ProviderStub = _BASE.ProviderStub
require = _BASE.require
utc_now = _BASE.utc_now

LOOPBACK = "127.0.0.1"
GATEWAY_PORT = 18800
CONTROL_PORT = 18801
PROVIDER_PORT = 18802
WEBHOOK_PORT = 18803
FIXED_PORTS = (GATEWAY_PORT, CONTROL_PORT, PROVIDER_PORT, WEBHOOK_PORT)
WORK_ROOT = ROOT / ".tmp" / "prd080-runtime"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd080-runtime"
MIGRATIONS_DIR = ROOT / "infra" / "migrations"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
OWNER_FILENAME = ".prd080-runtime-owner"
EXPECTED_MIGRATION_COUNT = 73
EXPECTED_MIGRATION_VERSION = "0073"
PREVIOUS_MIGRATION_VERSION = "0072"
MAX_WEBHOOK_BYTES = 16 * 1024

SOURCE_FILES = {
    "runtime_acceptance": Path(__file__).resolve(),
    "runtime_contract": ROOT
    / "scripts"
    / "acceptance"
    / "tests"
    / "test_prd080_runtime_acceptance.py",
    "runtime_helper": ROOT / "scripts" / "acceptance" / "prd079-runtime-acceptance.py",
    "migration_up": MIGRATIONS_DIR / "0073_billing_dispute_adjustment_workflow.sql",
    "migration_down": MIGRATIONS_DIR / "0073_billing_dispute_adjustment_workflow.down.sql",
    "dispute_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "billing_disputes"
    / "service.py",
    "dispute_outbox": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "billing_disputes"
    / "outbox.py",
    "dispute_scheduler": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "billing_disputes"
    / "scheduler.py",
    "webhook_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "webhooks"
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
        "gateway",
        "access_control",
        "disputes",
        "recovery",
        "database",
        "privacy",
        "cleanup",
        "source_provenance",
    }
)
EVIDENCE_SECTION_KEYS = {
    "services": frozenset(
        {
            "control_http",
            "gateway_http",
            "provider_loopback",
            "webhook_loopback",
            "control_restarted",
        }
    ),
    "gateway": frozenset(
        {
            "success_status",
            "successful_requests",
            "canonical_usage_rows",
            "canonical_charge_rows",
            "failure_status_class",
            "failed_request_rows",
            "provider_network_calls",
        }
    ),
    "access_control": frozenset(
        {
            "developer_status",
            "cross_tenant_status",
            "idempotency_conflict_status",
            "live_revocation_status",
            "error_contract_count",
        }
    ),
    "disputes": frozenset(
        {
            "request_scope_items",
            "time_range_items",
            "approved_count",
            "rejected_count",
            "transition_count",
            "released_hold_count",
            "adjustment_count",
            "reversal_count",
            "partial_approval_exact",
        }
    ),
    "recovery": frozenset(
        {
            "initial_failure_committed",
            "stable_delivery_id",
            "receiver_deduplicated",
            "webhook_network_deliveries",
            "webhook_logical_events",
            "duplicate_deliveries",
            "outbox_succeeded",
            "outbox_attempts",
            "pending_leases",
            "signatures_valid",
            "exact_payload_schema",
        }
    ),
    "database": frozenset(
        {
            "integrity",
            "foreign_key_violations",
            "audit_action_rows",
            "event_rows",
            "webhook_delivery_rows",
        }
    ),
    "privacy": frozenset({"files_scanned", "plaintext_matches", "sqlite_sidecars_remaining"}),
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
        "endpoint_url",
        "base_url",
        "database_path",
        "database_url",
        "encrypted_key",
        "ciphertext",
        "key_hash",
        "key_prefix",
        "idempotency_key",
        "request_id",
        "request_ids",
        "trace_id",
        "reason",
        "decision_reason",
    }
)
FORBIDDEN_WEBHOOK_KEYS = frozenset(
    {
        "authorization",
        "password",
        "token",
        "prompt",
        "response",
        "content",
        "provider_key",
        "signing_secret",
        "endpoint_url",
        "lease_token",
        "reason",
        "decision_reason",
    }
)

_DISPUTE_ID = re.compile(r"^bdsp_[0-9a-f]{32}$")
_EVENT_ID = re.compile(r"^bdevt_[0-9a-f]{32}$")
_WEBHOOK_DELIVERY_ID = re.compile(r"^whd_[0-9a-f]{32}$")
_MONEY = re.compile(r"^(?:0|[1-9][0-9]{0,15})\.[0-9]{6}$")
_UTC_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    """Keep every credential and content canary out of repr and evidence."""

    admin_password: str = field(repr=False)
    role_password: str = field(repr=False)
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
            return f"Prd080-{label}-{secrets.token_urlsafe(48)}"

        return cls(
            admin_password=value("AdminPassword-Aa1"),
            role_password=value("RolePassword-Aa1"),
            provider_key=value("ProviderCredential"),
            virtual_key=f"mhv_prd080_{secrets.token_urlsafe(44)}",
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
            self.role_password,
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
    other_tenant_id: str
    admin_email: str
    finance_email: str
    developer_email: str
    other_admin_email: str
    provider_id: str
    model_id: str
    model_alias: str
    migration: dict[str, dict[str, object]]


def safe_run_id(value: str | None) -> str:
    candidate = value or (datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ-") + secrets.token_hex(4))
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
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        require(not WORK_ROOT.is_symlink(), "config.work_root", "symlink")
        require(not ARTIFACT_ROOT.is_symlink(), "config.artifact_root", "symlink")
        work = _inside(WORK_ROOT / run_id, WORK_ROOT, stage="config.work")
        staging = _inside(
            ARTIFACT_ROOT / f".{run_id}.staging",
            ARTIFACT_ROOT,
            stage="config.artifact",
        )
        final = _inside(
            ARTIFACT_ROOT / run_id,
            ARTIFACT_ROOT,
            stage="config.artifact",
        )
        require(work.parent == WORK_ROOT.resolve(), "config.work", "parent")
        require(
            staging.parent == ARTIFACT_ROOT.resolve() and final.parent == ARTIFACT_ROOT.resolve(),
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
    _remove_owned_tree(work, WORK_ROOT, owner, stage="cleanup.work")


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
    database = _inside(work / "prd080-runtime.sqlite3", work, stage="database.path")
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
        version=PREVIOUS_MIGRATION_VERSION,
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

    finance_email = "finance-prd080@local.invalid"
    developer_email = "developer-prd080@local.invalid"
    other_admin_email = "admin-prd080-other@local.invalid"
    other_tenant_id = "tenant_prd080_runtime_other"
    now = utc_now()
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
        require(owner is not None, "database.business_seed", "provider_owner")
        provider_id = str(owner["provider_id"])
        model_id = str(owner["model_id"])
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
              provider_model_id='local-chat-model',input_price_per_1k=100,
              output_price_per_1k=100,currency='CNY',
              price_schema_version='prd080-runtime-v1',updated_at=?
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
        password_hash = hash_password(runtime.role_password)
        connection.executemany(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                (
                    "user_prd080_runtime_finance",
                    seed.tenant_id,
                    finance_email,
                    "PRD-080 Runtime Finance",
                    password_hash,
                    "finance_admin",
                    now,
                    now,
                ),
                (
                    "user_prd080_runtime_developer",
                    seed.tenant_id,
                    developer_email,
                    "PRD-080 Runtime Developer",
                    password_hash,
                    "developer",
                    now,
                    now,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,?,'PRD-080 Runtime Other','internal_only','active',?,?)
            """,
            (other_tenant_id, "prd080-runtime-other", now, now),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES('user_prd080_runtime_other_admin',?,?,?,?,'admin','active',?,?)
            """,
            (
                other_tenant_id,
                other_admin_email,
                "PRD-080 Runtime Other Admin",
                password_hash,
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
        other_tenant_id=other_tenant_id,
        admin_email=seed.admin_email,
        finance_email=finance_email,
        developer_email=developer_email,
        other_admin_email=other_admin_email,
        provider_id=provider_id,
        model_id=model_id,
        model_alias=seed.model_alias,
        migration=migration,
    )


def service_environment(
    work: Path,
    graph: RuntimeGraph | Any,
    runtime: RuntimeSecrets,
) -> dict[str, str]:
    environment = _BASE.service_environment(work, graph, runtime)
    environment.update(
        {
            "METAHUB_GATEWAY_API_BASE_URL": f"http://{LOOPBACK}:{GATEWAY_PORT}",
            "METAHUB_CONTROL_API_BASE_URL": f"http://{LOOPBACK}:{CONTROL_PORT}",
            "METAHUB_ALLOWED_ORIGINS": f"http://{LOOPBACK}:{CONTROL_PORT}",
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


def _contains_forbidden_webhook_key(value: object) -> bool:
    if type(value) is dict:
        return any(
            str(key).casefold() in FORBIDDEN_WEBHOOK_KEYS or _contains_forbidden_webhook_key(child)
            for key, child in value.items()
        )
    if type(value) is list:
        return any(_contains_forbidden_webhook_key(item) for item in value)
    return False


def _valid_webhook_payload(payload: object) -> bool:
    if type(payload) is not dict or set(payload) != {
        "schema_version",
        "billing_dispute_id",
        "scope_type",
        "item_count",
        "currency_totals",
        "status",
        "created_at",
    }:
        return False
    totals = payload.get("currency_totals")
    return (
        payload.get("schema_version") == 1
        and type(payload.get("billing_dispute_id")) is str
        and _DISPUTE_ID.fullmatch(str(payload["billing_dispute_id"])) is not None
        and payload.get("scope_type") in {"request_ids", "time_range"}
        and type(payload.get("item_count")) is int
        and 1 <= int(payload["item_count"]) <= 100
        and type(totals) is dict
        and 1 <= len(totals) <= 2
        and set(totals).issubset({"CNY", "USD"})
        and all(type(amount) is str and _MONEY.fullmatch(amount) for amount in totals.values())
        and payload.get("status") == "submitted"
        and type(payload.get("created_at")) is str
        and _UTC_TIME.fullmatch(str(payload["created_at"])) is not None
        and not _contains_forbidden_webhook_key(payload)
    )


def _valid_webhook_envelope(envelope: object) -> bool:
    if type(envelope) is not dict or set(envelope) != {
        "event_id",
        "event_type",
        "timestamp",
        "tenant_id",
        "actor_id",
        "object_id",
        "trace_id",
        "payload",
    }:
        return False
    payload = envelope.get("payload")
    return (
        envelope.get("event_type") == "billing_dispute.created"
        and type(envelope.get("event_id")) is str
        and _EVENT_ID.fullmatch(str(envelope["event_id"])) is not None
        and type(envelope.get("timestamp")) is str
        and _UTC_TIME.fullmatch(str(envelope["timestamp"])) is not None
        and all(
            type(envelope.get(name)) is str and bool(envelope.get(name))
            for name in ("tenant_id", "actor_id", "object_id", "trace_id")
        )
        and type(payload) is dict
        and envelope.get("object_id") == payload.get("billing_dispute_id")
        and _valid_webhook_payload(payload)
        and not _contains_forbidden_webhook_key(envelope)
    )


class WebhookStub:
    """Validate one signed event stream and expose only safe delivery counts."""

    def __init__(self, *, port: int) -> None:
        self.port = port
        self._lock = threading.Lock()
        self._secret: str | None = None
        self._recover = False
        self._crash_attempt_entered = threading.Event()
        self._release_crash_attempt = threading.Event()
        self._first_digest: str | None = None
        self._delivery_counts: dict[str, int] = {}
        self._delivery_ids: dict[str, set[str]] = {}
        self._logical_digests: set[str] = set()
        self.network_deliveries = 0
        self.invalid = 0
        self.signature_failures = 0
        self.forbidden_hits = 0
        self.schema_failures = 0
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def set_secret(self, value: str) -> None:
        require(len(value) >= 32, "webhook.secret", "invalid")
        self._secret = value

    def enable_recovery(self) -> None:
        with self._lock:
            self._recover = True
        self._release_crash_attempt.set()

    def wait_crash_attempt(self) -> None:
        require(
            self._crash_attempt_entered.wait(timeout=30),
            "webhook.failure_window",
            "timeout",
        )

    def start(self) -> str:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                try:
                    lengths = self.headers.get_all("Content-Length", failobj=[])
                    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                        raise ValueError("length")
                    length = int(lengths[0])
                    if not 0 < length <= MAX_WEBHOOK_BYTES:
                        raise ValueError("length")
                    body = self.rfile.read(length)
                    envelope = json.loads(body.decode("utf-8", errors="strict"))
                    event_type = envelope.get("event_type") if type(envelope) is dict else None
                    event_id = envelope.get("event_id") if type(envelope) is dict else None
                    delivery_id = self.headers.get("X-MetaHub-Delivery", "")
                    signature_ok = bool(owner._secret) and verify_webhook_signature(
                        self.headers.get("X-MetaHub-Signature", ""),
                        str(owner._secret),
                        body,
                    )
                    header_ok = self.headers.get("X-MetaHub-Event") == event_type
                    delivery_header_ok = _WEBHOOK_DELIVERY_ID.fullmatch(delivery_id) is not None
                    schema_ok = _valid_webhook_envelope(envelope)
                    forbidden = _contains_forbidden_webhook_key(envelope)
                    digest = (
                        hashlib.sha256(str(event_id).encode("ascii")).hexdigest()
                        if type(event_id) is str
                        else ""
                    )
                    del body, envelope
                except (UnicodeError, ValueError, json.JSONDecodeError):
                    with owner._lock:
                        owner.invalid += 1
                    self._reply(400)
                    return
                valid = (
                    event_type == "billing_dispute.created"
                    and bool(digest)
                    and signature_ok
                    and header_ok
                    and delivery_header_ok
                    and schema_ok
                    and not forbidden
                )
                hold_for_crash = False
                with owner._lock:
                    if not signature_ok:
                        owner.signature_failures += 1
                    if forbidden:
                        owner.forbidden_hits += 1
                    if not schema_ok or not header_ok or not delivery_header_ok:
                        owner.schema_failures += 1
                    if not valid:
                        owner.invalid += 1
                        recover = owner._recover
                    else:
                        owner.network_deliveries += 1
                        owner._delivery_counts[digest] = owner._delivery_counts.get(digest, 0) + 1
                        owner._delivery_ids.setdefault(digest, set()).add(delivery_id)
                        delivery_count = owner._delivery_counts[digest]
                        owner._logical_digests.add(digest)
                        if owner._first_digest is None:
                            owner._first_digest = digest
                        if digest == owner._first_digest and delivery_count == 2:
                            hold_for_crash = not owner._recover
                            if hold_for_crash:
                                owner._crash_attempt_entered.set()
                        recover = owner._recover
                if valid and not recover:
                    # Attempt 1 is already durable. Gate attempt 2 until the runner has
                    # killed its exact Control owner, so the pending recovery window is
                    # deterministic.
                    if hold_for_crash and not owner._release_crash_attempt.wait(timeout=60):
                        with owner._lock:
                            owner.invalid += 1
                    self._reply(503)
                else:
                    self._reply(204 if valid else 400)

            def _reply(self, status: int) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

            def log_message(self, _format: str, *_args: object) -> None:
                return

        try:
            self.server = ThreadingHTTPServer((LOOPBACK, self.port), Handler)
            self.server.daemon_threads = True
        except OSError:
            raise AcceptanceFailure("webhook", "port_unavailable") from None
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="prd080-runtime-webhook",
            daemon=True,
        )
        self.thread.start()
        return f"http://{LOOPBACK}:{self.port}/events"

    def stop(self) -> None:
        self._release_crash_attempt.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def snapshot(self, expected_first_delivery_id: str) -> dict[str, int | bool]:
        with self._lock:
            first_count = (
                self._delivery_counts.get(self._first_digest, 0)
                if self._first_digest is not None
                else 0
            )
            logical = len(self._logical_digests)
            first_ids = self._delivery_ids.get(self._first_digest or "", set())
            all_ids = set().union(*self._delivery_ids.values()) if self._delivery_ids else set()
            stable_delivery = (
                _WEBHOOK_DELIVERY_ID.fullmatch(expected_first_delivery_id) is not None
                and first_ids == {expected_first_delivery_id}
                and len(self._delivery_ids) == logical
                and all(len(values) == 1 for values in self._delivery_ids.values())
                and len(all_ids) == logical
            )
            return {
                "network_deliveries": self.network_deliveries,
                "logical_events": logical,
                "duplicate_deliveries": self.network_deliveries - logical,
                "first_event_deliveries": first_count,
                "stable_delivery_id": stable_delivery,
                "invalid": self.invalid,
                "signature_failures": self.signature_failures,
                "forbidden_hits": self.forbidden_hits,
                "schema_failures": self.schema_failures,
            }


def _request_headers(
    token: str,
    request_id: str,
    *,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": request_id,
        "X-Trace-ID": f"trace-{request_id}",
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _json_object(response: httpx.Response, *, stage: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise AcceptanceFailure(stage, "json") from None
    require(isinstance(payload, dict), stage, "shape")
    return payload


def _require_private(
    response: httpx.Response,
    *,
    status: int,
    request_id: str,
    stage: str,
    code: str | None = None,
) -> dict[str, Any]:
    require(response.status_code == status, stage, "status")
    require(
        response.headers.get("cache-control") == "no-store, private",
        stage,
        "cache_control",
    )
    payload = _json_object(response, stage=stage)
    response_request_id = response.headers.get("x-request-id")
    response_trace_id = response.headers.get("x-trace-id")
    require(
        isinstance(response_request_id, str)
        and response_request_id.startswith("req_")
        and payload.get("request_id") == response_request_id
        and response_request_id != request_id,
        stage,
        "request_id",
    )
    require(
        isinstance(response_trace_id, str)
        and response_trace_id.startswith("trc_")
        and payload.get("trace_id") == response_trace_id
        and response_trace_id != request_id,
        stage,
        "trace_id",
    )
    if code is not None:
        require(payload.get("code") == code, stage, "code")
    return payload


def _login(
    client: httpx.Client,
    *,
    email: str,
    password: str,
    request_id: str,
    tenant_slug: str = "default",
) -> str:
    response = client.post(
        "/auth/login",
        headers={"X-Request-ID": request_id},
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    require(response.status_code == 200, "http.login", "status")
    payload = _json_object(response, stage="http.login")
    token = payload.get("access_token")
    require(isinstance(token, str) and len(token) > 40, "http.login", "token")
    return token


def _fund_quota(client: httpx.Client, token: str) -> str:
    trial_end = (
        (datetime.now(UTC).replace(microsecond=0) + timedelta(days=2))
        .isoformat()
        .replace("+00:00", "Z")
    )
    plan_request_id = "prd080-runtime-plan-create"
    plan_response = client.patch(
        "/plans",
        headers=_request_headers(token, plan_request_id),
        json={
            "expected_revision": 0,
            "display_name": "PRD-080 Runtime Plan",
            "base_tier": "paid",
            "status": "active",
            "trial_ends_at": trial_end,
        },
    )
    require(plan_response.status_code == 200, "http.plan", "status")
    plan_payload = _json_object(plan_response, stage="http.plan")
    require(plan_payload.get("request_id") == plan_request_id, "http.plan", "request_id")
    buckets = plan_payload.get("quota_buckets")
    require(isinstance(buckets, list), "http.plan", "buckets")
    paid = next(
        (item for item in buckets if isinstance(item, dict) and item.get("quota_type") == "paid"),
        None,
    )
    require(isinstance(paid, dict), "http.plan", "paid_bucket")
    revision = paid.get("state_revision")
    require(type(revision) is int, "http.plan", "bucket_revision")
    adjustment_key = f"prd080-runtime-funding-{secrets.token_urlsafe(24)}"
    adjustment_request_id = "prd080-runtime-quota-credit"
    adjustment = client.post(
        "/quota-adjustments",
        headers=_request_headers(
            token,
            adjustment_request_id,
            idempotency_key=adjustment_key,
        ),
        json={
            "quota_type": "paid",
            "direction": "credit",
            "amount": "20.000000",
            "reason": "PRD-080 bounded runtime funding",
            "expected_bucket_revision": revision,
        },
    )
    require(adjustment.status_code == 201, "http.quota_credit", "status")
    adjustment_payload = _json_object(adjustment, stage="http.quota_credit")
    require(
        adjustment_payload.get("request_id") == adjustment_request_id,
        "http.quota_credit",
        "request_id",
    )
    return adjustment_key


def _invoke_gateway(
    graph: RuntimeGraph,
    runtime: RuntimeSecrets,
    *,
    request_id: str,
    expect_success: bool,
) -> int:
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
            timeout=httpx.Timeout(40, connect=3),
            follow_redirects=False,
            trust_env=False,
        )
    except httpx.HTTPError:
        raise AcceptanceFailure("http.gateway", "transport") from None
    payload = _json_object(response, stage="http.gateway")
    if expect_success:
        require(response.status_code == 200, "http.gateway", "success_status")
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
        require(400 <= response.status_code <= 599, "http.gateway", "failure_status")
        require(
            isinstance(payload.get("code"), str) and payload.get("request_id") == request_id,
            "http.gateway",
            "failure_contract",
        )
    return response.status_code


def _wait_database(
    graph: RuntimeGraph,
    predicate: Callable[[sqlite3.Connection], bool],
    *,
    stage: str,
    timeout_seconds: float = 35,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        connection = connect_database(graph.database_url)
        try:
            if predicate(connection):
                return
        finally:
            connection.close()
        time.sleep(0.1)
    raise AcceptanceFailure(stage, "timeout")


def _next_database_second(graph: RuntimeGraph) -> str:
    connection = connect_database(graph.database_url)
    try:
        initial = int(connection.execute("SELECT unixepoch('now')").fetchone()[0])
    finally:
        connection.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        connection = connect_database(graph.database_url)
        try:
            row = connection.execute(
                "SELECT unixepoch('now') AS value,strftime('%Y-%m-%dT%H:%M:%SZ','now') AS text"
            ).fetchone()
            if row is not None and int(row["value"]) > initial:
                return str(row["text"])
        finally:
            connection.close()
        time.sleep(0.05)
    raise AcceptanceFailure("database.clock", "timeout")


def _period_end_after_charges(graph: RuntimeGraph, request_ids: tuple[str, ...]) -> str:
    placeholders = ",".join("?" for _ in request_ids)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        connection = connect_database(graph.database_url)
        try:
            row = connection.execute(
                f"""
                SELECT MAX(unixepoch(created_at)) AS latest,unixepoch('now') AS current,
                       strftime('%Y-%m-%dT%H:%M:%SZ','now') AS current_text
                FROM finops_ledger_entries
                WHERE tenant_id=? AND entry_type='usage_charge'
                  AND source_request_id IN({placeholders})
                """,
                (graph.tenant_id, *request_ids),
            ).fetchone()
            if (
                row is not None
                and row["latest"] is not None
                and int(row["current"]) > int(row["latest"])
            ):
                return str(row["current_text"])
        finally:
            connection.close()
        time.sleep(0.05)
    raise AcceptanceFailure("database.period_end", "timeout")


def _canonical_charge_counts(
    graph: RuntimeGraph,
    request_ids: tuple[str, ...],
) -> tuple[int, int]:
    placeholders = ",".join("?" for _ in request_ids)
    connection = connect_database(graph.database_url)
    try:
        usage = int(
            connection.execute(
                f"""
                SELECT COUNT(*) FROM usage_ledger
                WHERE tenant_id=? AND request_id IN({placeholders})
                """,
                (graph.tenant_id, *request_ids),
            ).fetchone()[0]
        )
        charges = int(
            connection.execute(
                f"""
                SELECT COUNT(*) FROM finops_ledger_entries
                WHERE tenant_id=? AND entry_type='usage_charge'
                  AND source_request_id IN({placeholders})
                """,
                (graph.tenant_id, *request_ids),
            ).fetchone()[0]
        )
        distinct_usage = int(
            connection.execute(
                f"""
                SELECT COUNT(DISTINCT usage_ledger_id) FROM finops_ledger_entries
                WHERE tenant_id=? AND entry_type='usage_charge'
                  AND source_request_id IN({placeholders})
                """,
                (graph.tenant_id, *request_ids),
            ).fetchone()[0]
        )
        require(
            usage == charges == distinct_usage == len(request_ids),
            "database.canonical_charges",
            "counts",
        )
        return usage, charges
    finally:
        connection.close()


def _failed_request_rows(graph: RuntimeGraph, request_id: str) -> int:
    connection = connect_database(graph.database_url)
    try:
        return int(
            connection.execute(
                """
                SELECT COUNT(*) FROM request_logs
                WHERE tenant_id=? AND request_id=? AND status='failed'
                """,
                (graph.tenant_id, request_id),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _create_webhook(
    client: httpx.Client,
    token: str,
    webhook_url: str,
    stub: WebhookStub,
) -> str:
    request_id = "prd080-runtime-webhook-create"
    response = client.post(
        "/webhooks",
        headers=_request_headers(token, request_id),
        json={
            "name": "PRD-080 runtime receiver",
            "endpoint_url": webhook_url,
            "event_types": ["billing_dispute.created"],
            "status": "active",
            "timeout_ms": 5000,
        },
    )
    require(response.status_code == 200, "http.webhook", "status")
    payload = _json_object(response, stage="http.webhook")
    signing_secret = payload.get("signing_secret")
    require(isinstance(signing_secret, str), "http.webhook", "secret")
    stub.set_secret(signing_secret)
    return signing_secret


def _create_dispute(
    client: httpx.Client,
    token: str,
    payload: dict[str, object],
    *,
    key: str,
    request_id: str,
    expected_status: int = 201,
    expected_code: str | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/billing-disputes",
        headers=_request_headers(token, request_id, idempotency_key=key),
        json=payload,
    )
    return _require_private(
        response,
        status=expected_status,
        request_id=request_id,
        stage="http.dispute_create",
        code=expected_code,
    )


def _transition_dispute(
    client: httpx.Client,
    token: str,
    dispute_id: str,
    payload: dict[str, object],
    *,
    key: str,
    request_id: str,
    expected_status: int = 200,
    expected_code: str | None = None,
) -> dict[str, Any]:
    response = client.patch(
        f"/billing-disputes/{dispute_id}",
        headers=_request_headers(token, request_id, idempotency_key=key),
        json=payload,
    )
    return _require_private(
        response,
        status=expected_status,
        request_id=request_id,
        stage="http.dispute_transition",
        code=expected_code,
    )


def _revoke_finops_write(graph: RuntimeGraph) -> None:
    connection = connect_database(graph.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        role = connection.execute(
            """
            SELECT role_id,permission_revision FROM roles
            WHERE tenant_id=? AND code='finance_admin'
            """,
            (graph.tenant_id,),
        ).fetchone()
        permission = connection.execute(
            """
            SELECT permission_id FROM permissions
            WHERE tenant_id=? AND code='billing.finops.write'
            """,
            (graph.tenant_id,),
        ).fetchone()
        require(role is not None and permission is not None, "database.rbac", "owner")
        deleted = connection.execute(
            """
            DELETE FROM role_permissions
            WHERE tenant_id=? AND role_id=? AND permission_id=?
            """,
            (graph.tenant_id, role["role_id"], permission["permission_id"]),
        )
        require(deleted.rowcount == 1, "database.rbac", "delete")
        remaining = [
            str(row["code"])
            for row in connection.execute(
                """
                SELECT permission.code FROM role_permissions relation
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE relation.tenant_id=? AND relation.role_id=?
                ORDER BY permission.code
                """,
                (graph.tenant_id, role["role_id"]),
            ).fetchall()
        ]
        updated = connection.execute(
            """
            UPDATE roles
            SET permission_revision=?,matrix_sha256=?,
                updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
            WHERE tenant_id=? AND role_id=?
            """,
            (
                int(role["permission_revision"]) + 1,
                permission_matrix_sha256(remaining),
                graph.tenant_id,
                role["role_id"],
            ),
        )
        require(updated.rowcount == 1, "database.rbac", "revision")
        connection.commit()
    except AcceptanceFailure:
        connection.rollback()
        raise
    except sqlite3.Error:
        connection.rollback()
        raise AcceptanceFailure("database.rbac", "failed") from None
    finally:
        connection.close()


def _outbox_row(connection: sqlite3.Connection, dispute_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT outbox.status,outbox.attempt_count,outbox.lease_owner_id,
               outbox.lease_token,outbox.lease_expires_at,outbox.delivery_snapshot_json,
               delivery.webhook_delivery_id,delivery.status AS delivery_status,
               delivery.attempt_count AS delivery_attempt_count
        FROM billing_disputes dispute
        JOIN billing_dispute_events event
          ON event.tenant_id=dispute.tenant_id
         AND event.billing_dispute_id=dispute.billing_dispute_id
        JOIN billing_dispute_event_outbox outbox
          ON outbox.tenant_id=event.tenant_id
         AND outbox.billing_dispute_event_id=event.billing_dispute_event_id
        JOIN webhook_deliveries delivery
          ON delivery.tenant_id=event.tenant_id
         AND delivery.event_id=event.billing_dispute_event_id
         AND delivery.delivery_kind='original'
        WHERE dispute.billing_dispute_id=?
        """,
        (dispute_id,),
    ).fetchone()


def _delivery_attempt_rows(
    connection: sqlite3.Connection,
    delivery_id: str,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT attempt_no,status,http_status,error_code
        FROM webhook_delivery_attempts
        WHERE webhook_delivery_id=?
        ORDER BY attempt_no
        """,
        (delivery_id,),
    ).fetchall()


def _initial_failure_committed(graph: RuntimeGraph, dispute_id: str) -> str | None:
    connection = connect_database(graph.database_url)
    try:
        row = _outbox_row(connection, dispute_id)
        if row is None:
            return None
        delivery_id = str(row["webhook_delivery_id"])
        try:
            snapshot = json.loads(str(row["delivery_snapshot_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        attempts = _delivery_attempt_rows(connection, delivery_id)
        valid = (
            row["status"] == "processing"
            and row["attempt_count"] == 0
            and row["lease_owner_id"] is not None
            and row["lease_token"] is not None
            and row["lease_expires_at"] is not None
            and row["delivery_status"] == "pending"
            and row["delivery_attempt_count"] == 1
            and snapshot == [delivery_id]
            and len(attempts) == 1
            and attempts[0]["attempt_no"] == 1
            and attempts[0]["status"] == "failed"
            and attempts[0]["http_status"] == 503
            and attempts[0]["error_code"] == "webhook.http_error"
        )
        return delivery_id if valid and _WEBHOOK_DELIVERY_ID.fullmatch(delivery_id) else None
    finally:
        connection.close()


def _first_recovery_succeeded(
    graph: RuntimeGraph,
    dispute_id: str,
    expected_delivery_id: str,
) -> bool:
    connection = connect_database(graph.database_url)
    try:
        row = _outbox_row(connection, dispute_id)
        if row is None or row["webhook_delivery_id"] != expected_delivery_id:
            return False
        attempts = _delivery_attempt_rows(connection, expected_delivery_id)
        return bool(
            row["status"] == "succeeded"
            and row["attempt_count"] == 1
            and row["lease_owner_id"] is None
            and row["lease_token"] is None
            and row["lease_expires_at"] is None
            and row["delivery_status"] == "success"
            and row["delivery_attempt_count"] == 2
            and [
                (
                    attempt["attempt_no"],
                    attempt["status"],
                    attempt["http_status"],
                    attempt["error_code"],
                )
                for attempt in attempts
            ]
            == [
                (1, "failed", 503, "webhook.http_error"),
                (2, "success", 204, None),
            ]
        )
    finally:
        connection.close()


def _final_database_facts(
    graph: RuntimeGraph,
    *,
    success_ids: tuple[str, ...],
    failure_id: str,
    request_dispute_id: str,
    time_dispute_id: str,
) -> dict[str, object]:
    connection = connect_database(graph.database_url)
    try:
        dispute_counts = {
            str(row["status"]): int(row["value"])
            for row in connection.execute(
                """
                SELECT status,COUNT(*) AS value FROM billing_disputes
                WHERE tenant_id=? GROUP BY status
                """,
                (graph.tenant_id,),
            ).fetchall()
        }
        action_counts = {
            str(row["action"]): int(row["value"])
            for row in connection.execute(
                """
                SELECT action,COUNT(*) AS value FROM billing_dispute_transitions
                WHERE tenant_id=? GROUP BY action
                """,
                (graph.tenant_id,),
            ).fetchall()
        }
        item_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM billing_dispute_items WHERE tenant_id=?",
                (graph.tenant_id,),
            ).fetchone()[0]
        )
        released_holds = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM billing_settlement_holds
                WHERE tenant_id=? AND status='released' AND state_revision=2
                  AND release_reason IN('approved','rejected')
                """,
                (graph.tenant_id,),
            ).fetchone()[0]
        )
        active_holds = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM billing_settlement_holds
                WHERE tenant_id=? AND status='active'
                """,
                (graph.tenant_id,),
            ).fetchone()[0]
        )
        adjustment_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM billing_dispute_adjustment_links
                WHERE tenant_id=?
                """,
                (graph.tenant_id,),
            ).fetchone()[0]
        )
        reversal = connection.execute(
            """
            SELECT COUNT(*) AS value,MIN(entry.amount_micros) AS minimum,
                   MAX(entry.amount_micros) AS maximum
            FROM billing_dispute_adjustment_links link
            JOIN finops_adjustments adjustment
              ON adjustment.tenant_id=link.tenant_id
             AND adjustment.finops_adjustment_id=link.finops_adjustment_id
            JOIN finops_ledger_entries entry
              ON entry.tenant_id=link.tenant_id
             AND entry.finops_ledger_entry_id=link.reversal_finops_ledger_entry_id
            WHERE link.tenant_id=? AND adjustment.adjustment_type='reversal'
              AND entry.entry_type='reversal'
            """,
            (graph.tenant_id,),
        ).fetchone()
        require(reversal is not None, "database.reversal", "missing")
        reversal_count = int(reversal["value"])
        partial_exact = (
            reversal_count == 1
            and int(reversal["minimum"]) == 500_000
            and int(reversal["maximum"]) == 500_000
        )
        audit_counts = {
            str(row["action"]): int(row["value"])
            for row in connection.execute(
                """
                SELECT action,COUNT(*) AS value FROM audit_logs
                WHERE tenant_id=? AND action IN(
                  'billing_dispute.created','billing_dispute.review_started',
                  'billing_dispute.approved','billing_dispute.rejected',
                  'finops.adjustment_created'
                ) GROUP BY action
                """,
                (graph.tenant_id,),
            ).fetchall()
        }
        event_rows = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM billing_dispute_events
                WHERE tenant_id=? AND event_type='billing_dispute.created'
                """,
                (graph.tenant_id,),
            ).fetchone()[0]
        )
        outbox = connection.execute(
            """
            SELECT COUNT(*) AS value,SUM(attempt_count) AS attempts,
                   SUM(status='succeeded') AS succeeded,
                   SUM(status='processing' OR lease_owner_id IS NOT NULL
                     OR lease_token IS NOT NULL OR lease_expires_at IS NOT NULL) AS leases
            FROM billing_dispute_event_outbox WHERE tenant_id=?
            """,
            (graph.tenant_id,),
        ).fetchone()
        deliveries = connection.execute(
            """
            SELECT COUNT(*) AS value,SUM(attempt_count) AS attempts,
                   SUM(status='success') AS succeeded
            FROM webhook_deliveries
            WHERE tenant_id=? AND event_type='billing_dispute.created'
              AND delivery_kind='original'
            """,
            (graph.tenant_id,),
        ).fetchone()
        request_items = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM billing_dispute_items
                WHERE tenant_id=? AND billing_dispute_id=?
                """,
                (graph.tenant_id, request_dispute_id),
            ).fetchone()[0]
        )
        time_items = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM billing_dispute_items
                WHERE tenant_id=? AND billing_dispute_id=?
                """,
                (graph.tenant_id, time_dispute_id),
            ).fetchone()[0]
        )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()

    usage_rows, charge_rows = _canonical_charge_counts(graph, success_ids)
    failed_rows = _failed_request_rows(graph, failure_id)
    expected_audits = {
        "billing_dispute.created": 2,
        "billing_dispute.review_started": 2,
        "billing_dispute.approved": 1,
        "billing_dispute.rejected": 1,
        "finops.adjustment_created": 1,
    }
    require(dispute_counts == {"approved": 1, "rejected": 1}, "database.disputes", "status")
    require(
        action_counts == {"approve": 1, "create": 2, "reject": 1, "start_review": 2},
        "database.transitions",
        "actions",
    )
    require(item_count == 4, "database.items", "count")
    require(request_items == 2 and time_items == 2, "database.items", "scope_counts")
    require(released_holds == 4 and active_holds == 0, "database.holds", "counts")
    require(
        adjustment_count == reversal_count == 1 and partial_exact, "database.adjustment", "counts"
    )
    require(audit_counts == expected_audits, "database.audit", "counts")
    require(event_rows == 2, "database.events", "count")
    require(
        outbox is not None
        and int(outbox["value"]) == int(outbox["succeeded"]) == 2
        and int(outbox["attempts"]) == 2
        and int(outbox["leases"]) == 0,
        "database.outbox",
        "counts",
    )
    require(
        deliveries is not None
        and int(deliveries["value"]) == int(deliveries["succeeded"]) == 2
        and int(deliveries["attempts"]) == 3,
        "database.webhook_deliveries",
        "counts",
    )
    require(usage_rows == charge_rows == 4, "database.gateway", "canonical")
    require(failed_rows == 1, "database.gateway", "failed_request")
    require(integrity == "ok" and foreign_keys == 0, "database.final", "integrity")
    return {
        "gateway": {
            "canonical_usage_rows": usage_rows,
            "canonical_charge_rows": charge_rows,
            "failed_request_rows": failed_rows,
        },
        "disputes": {
            "request_scope_items": request_items,
            "time_range_items": time_items,
            "approved_count": dispute_counts["approved"],
            "rejected_count": dispute_counts["rejected"],
            "transition_count": sum(action_counts.values()),
            "released_hold_count": released_holds,
            "adjustment_count": adjustment_count,
            "reversal_count": reversal_count,
            "partial_approval_exact": partial_exact,
        },
        "recovery": {
            "outbox_succeeded": int(outbox["succeeded"]),
            "outbox_attempts": int(outbox["attempts"]),
            "pending_leases": int(outbox["leases"]),
        },
        "database": {
            "integrity": integrity,
            "foreign_key_violations": foreign_keys,
            "audit_action_rows": sum(audit_counts.values()),
            "event_rows": event_rows,
            "webhook_delivery_rows": int(deliveries["value"]),
        },
    }


def _close_runtime_owners(
    processes: OwnedProcesses,
    provider: ProviderStub,
    webhook: WebhookStub,
) -> None:
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
    try:
        webhook.stop()
    except Exception:
        if first_failure is None:
            first_failure = AcceptanceFailure("cleanup.owners", "webhook")
    if first_failure is not None:
        raise first_failure


def _port_available(port: int) -> bool:
    """Probe one fixed loopback port without enumerating or stopping ambient processes."""

    candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        candidate.bind((LOOPBACK, port))
        return True
    except OSError:
        return False
    finally:
        candidate.close()


def _crash_owned_control(processes: OwnedProcesses, control: Any) -> None:
    """Crash the exact owned Control tree and prove its listener is gone before recovery."""

    require(control in processes.items, "recovery.control_crash", "owner")
    process = control.process
    require(
        isinstance(process.pid, int) and process.pid > 0 and process.poll() is None,
        "recovery.control_crash",
        "process",
    )
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            if result.returncode != 0 and process.poll() is None:
                raise AcceptanceFailure("recovery.control_crash", "tree")
        else:
            process.kill()
        _PROCESS.stop_owned_process(control)
    except AcceptanceFailure:
        raise
    except (OSError, subprocess.SubprocessError, ValueError):
        raise AcceptanceFailure("recovery.control_crash", "tree") from None
    if control in processes.items:
        processes.items.remove(control)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _port_available(CONTROL_PORT):
            return
        time.sleep(0.1)
    raise AcceptanceFailure("recovery.control_crash", "port")


def _exercise_http_runtime(
    work: Path,
    graph: RuntimeGraph,
    environment: dict[str, str],
    runtime: RuntimeSecrets,
    provider: ProviderStub,
    webhook: WebhookStub,
    webhook_url: str,
    processes: OwnedProcesses,
) -> tuple[dict[str, object], tuple[str, ...], tuple[str, ...]]:
    gateway = processes.add(
        _PROCESS.start_service(
            "gateway",
            "metahub_gateway.app:app",
            GATEWAY_PORT,
            work=work,
            environment=environment,
        )
    )
    _PROCESS.wait_health(
        f"http://{LOOPBACK}:{GATEWAY_PORT}/health",
        gateway,
        stage="health.gateway",
    )
    control = processes.add(
        _PROCESS.start_service(
            "control-initial",
            "metahub_control.app:app",
            CONTROL_PORT,
            work=work,
            environment=environment,
        )
    )
    _PROCESS.wait_health(
        f"http://{LOOPBACK}:{CONTROL_PORT}/health",
        control,
        stage="health.control_initial",
    )

    success_ids = tuple(f"prd080-runtime-gateway-success-{ordinal:02d}" for ordinal in range(1, 5))
    failure_id = "prd080-runtime-gateway-failure"
    request_reason = "Runtime request charges require bounded review"
    conflict_reason = "Changed runtime intent must conflict"
    time_reason = "Runtime time range requires bounded review"
    approve_reason = "Approve one bounded partial reversal"
    rejected_reason = "Canonical charges remain valid"
    denied_reason = "This write must observe live permission revocation"
    operation_ids = (
        "prd080-runtime-login-admin",
        "prd080-runtime-login-finance",
        "prd080-runtime-login-developer",
        "prd080-runtime-login-other",
        "prd080-runtime-plan-create",
        "prd080-runtime-quota-credit",
        "prd080-runtime-webhook-create",
        "prd080-runtime-developer-denied",
        "prd080-runtime-request-create",
        "prd080-runtime-request-replay",
        "prd080-runtime-request-conflict",
        "prd080-runtime-cross-tenant",
        "prd080-runtime-request-start",
        "prd080-runtime-request-approve",
        "prd080-runtime-time-create",
        "prd080-runtime-time-start",
        "prd080-runtime-live-revocation",
        "prd080-runtime-time-reject",
        *success_ids,
        failure_id,
    )
    keys = {
        name: f"prd080-runtime-{name}-{secrets.token_urlsafe(24)}"
        for name in (
            "request-create",
            "request-start",
            "request-approve",
            "time-create",
            "time-start",
            "live-revocation",
            "time-reject",
        )
    }
    request_dispute_id = ""
    time_dispute_id = ""
    first_item_id = ""
    storage_private: list[str] = list(keys.values())
    evidence_private = list(operation_ids) + [
        request_reason,
        conflict_reason,
        time_reason,
        approve_reason,
        rejected_reason,
        denied_reason,
    ]

    with httpx.Client(
        base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
        timeout=httpx.Timeout(25, connect=3),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        admin_token = _login(
            client,
            email=graph.admin_email,
            password=runtime.admin_password,
            request_id=operation_ids[0],
        )
        finance_token = _login(
            client,
            email=graph.finance_email,
            password=runtime.role_password,
            request_id=operation_ids[1],
        )
        developer_token = _login(
            client,
            email=graph.developer_email,
            password=runtime.role_password,
            request_id=operation_ids[2],
        )
        other_token = _login(
            client,
            email=graph.other_admin_email,
            password=runtime.role_password,
            request_id=operation_ids[3],
            tenant_slug="prd080-runtime-other",
        )
        storage_private.extend((admin_token, finance_token, developer_token, other_token))
        storage_private.append(_fund_quota(client, admin_token))

        for request_id in success_ids[:2]:
            _invoke_gateway(graph, runtime, request_id=request_id, expect_success=True)
        period_start = _next_database_second(graph)
        for request_id in success_ids[2:]:
            _invoke_gateway(graph, runtime, request_id=request_id, expect_success=True)
        period_end = _period_end_after_charges(graph, success_ids[2:])
        _canonical_charge_counts(graph, success_ids)

        provider.fail_enabled = True
        try:
            failure_status = _invoke_gateway(
                graph,
                runtime,
                request_id=failure_id,
                expect_success=False,
            )
        finally:
            provider.fail_enabled = False
        _wait_database(
            graph,
            lambda _connection: _failed_request_rows(graph, failure_id) == 1,
            stage="database.failed_request",
        )

        signing_secret = _create_webhook(client, admin_token, webhook_url, webhook)
        storage_private.append(signing_secret)
        denied_id = "prd080-runtime-developer-denied"
        denied = client.get(
            "/billing-disputes",
            headers=_request_headers(developer_token, denied_id),
        )
        _require_private(
            denied,
            status=403,
            request_id=denied_id,
            stage="http.developer_denied",
            code="auth.permission_denied",
        )

        request_payload: dict[str, object] = {
            "scope_type": "request_ids",
            "request_ids": list(success_ids[:2]),
            "reason": request_reason,
        }
        created = _create_dispute(
            client,
            admin_token,
            request_payload,
            key=keys["request-create"],
            request_id="prd080-runtime-request-create",
        )
        dispute = created.get("dispute")
        require(isinstance(dispute, dict), "http.request_create", "dispute")
        summary = dispute.get("summary")
        items = dispute.get("items")
        require(
            isinstance(summary, dict) and isinstance(items, list) and len(items) == 2,
            "http.request_create",
            "shape",
        )
        request_dispute_id = str(summary.get("billing_dispute_id"))
        require(_DISPUTE_ID.fullmatch(request_dispute_id) is not None, "http.request_create", "id")
        first_item = items[0]
        require(isinstance(first_item, dict), "http.request_create", "item")
        first_item_id = str(first_item.get("billing_dispute_item_id"))

        replay = _create_dispute(
            client,
            admin_token,
            request_payload,
            key=keys["request-create"],
            request_id="prd080-runtime-request-replay",
            expected_status=200,
        )
        require(replay.get("idempotent_replay") is True, "http.request_replay", "flag")
        conflict_payload = dict(request_payload)
        conflict_payload["reason"] = conflict_reason
        _create_dispute(
            client,
            admin_token,
            conflict_payload,
            key=keys["request-create"],
            request_id="prd080-runtime-request-conflict",
            expected_status=409,
            expected_code="billing_dispute.idempotency_conflict",
        )

        cross_id = "prd080-runtime-cross-tenant"
        cross = client.get(
            f"/billing-disputes/{request_dispute_id}",
            headers=_request_headers(other_token, cross_id),
        )
        _require_private(
            cross,
            status=404,
            request_id=cross_id,
            stage="http.cross_tenant",
            code="billing_dispute.not_found",
        )
        webhook.wait_crash_attempt()

    _crash_owned_control(processes, control)
    first_delivery_id = _initial_failure_committed(graph, request_dispute_id)
    require(
        first_delivery_id is not None,
        "recovery.initial_failure",
        "state",
    )
    webhook.enable_recovery()
    recovered = processes.add(
        _PROCESS.start_service(
            "control-recovery",
            "metahub_control.app:app",
            CONTROL_PORT,
            work=work,
            environment=environment,
        )
    )
    _PROCESS.wait_health(
        f"http://{LOOPBACK}:{CONTROL_PORT}/health",
        recovered,
        stage="health.control_recovery",
    )
    _wait_database(
        graph,
        lambda _connection: _first_recovery_succeeded(
            graph,
            request_dispute_id,
            str(first_delivery_id),
        ),
        stage="recovery.first_outbox",
        timeout_seconds=115,
    )

    with httpx.Client(
        base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
        timeout=httpx.Timeout(25, connect=3),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        _transition_dispute(
            client,
            finance_token,
            request_dispute_id,
            {"action": "start_review", "expected_revision": 1},
            key=keys["request-start"],
            request_id="prd080-runtime-request-start",
        )
        approved = _transition_dispute(
            client,
            finance_token,
            request_dispute_id,
            {
                "action": "approve",
                "expected_revision": 2,
                "decision_reason": approve_reason,
                "adjustments": [
                    {
                        "billing_dispute_item_id": first_item_id,
                        "amount": "0.500000",
                    }
                ],
            },
            key=keys["request-approve"],
            request_id="prd080-runtime-request-approve",
        )
        approved_dispute = approved.get("dispute")
        approved_summary = (
            approved_dispute.get("summary") if isinstance(approved_dispute, dict) else None
        )
        require(
            isinstance(approved_summary, dict)
            and approved_summary.get("status") == "approved"
            and approved_summary.get("active_hold_count") == 0
            and approved_summary.get("adjustment_count") == 1,
            "http.request_approve",
            "summary",
        )

        time_payload: dict[str, object] = {
            "scope_type": "time_range",
            "period_start": period_start,
            "period_end": period_end,
            "reason": time_reason,
        }
        time_created = _create_dispute(
            client,
            admin_token,
            time_payload,
            key=keys["time-create"],
            request_id="prd080-runtime-time-create",
        )
        time_dispute = time_created.get("dispute")
        time_summary = time_dispute.get("summary") if isinstance(time_dispute, dict) else None
        time_items = time_dispute.get("items") if isinstance(time_dispute, dict) else None
        require(
            isinstance(time_summary, dict)
            and isinstance(time_items, list)
            and len(time_items) == 2,
            "http.time_create",
            "shape",
        )
        time_dispute_id = str(time_summary.get("billing_dispute_id"))
        require(_DISPUTE_ID.fullmatch(time_dispute_id) is not None, "http.time_create", "id")
        _transition_dispute(
            client,
            finance_token,
            time_dispute_id,
            {"action": "start_review", "expected_revision": 1},
            key=keys["time-start"],
            request_id="prd080-runtime-time-start",
        )
        _revoke_finops_write(graph)
        denied_item = time_items[0]
        require(isinstance(denied_item, dict), "http.live_revocation", "item")
        _transition_dispute(
            client,
            finance_token,
            time_dispute_id,
            {
                "action": "approve",
                "expected_revision": 2,
                "decision_reason": denied_reason,
                "adjustments": [
                    {
                        "billing_dispute_item_id": denied_item.get("billing_dispute_item_id"),
                        "amount": "0.100000",
                    }
                ],
            },
            key=keys["live-revocation"],
            request_id="prd080-runtime-live-revocation",
            expected_status=403,
            expected_code="auth.permission_denied",
        )
        rejected = _transition_dispute(
            client,
            finance_token,
            time_dispute_id,
            {
                "action": "reject",
                "expected_revision": 2,
                "decision_reason": rejected_reason,
            },
            key=keys["time-reject"],
            request_id="prd080-runtime-time-reject",
        )
        rejected_dispute = rejected.get("dispute")
        rejected_summary = (
            rejected_dispute.get("summary") if isinstance(rejected_dispute, dict) else None
        )
        require(
            isinstance(rejected_summary, dict)
            and rejected_summary.get("status") == "rejected"
            and rejected_summary.get("active_hold_count") == 0,
            "http.time_reject",
            "summary",
        )

    _wait_database(
        graph,
        lambda connection: (
            int(
                connection.execute(
                    """
                SELECT COUNT(*) FROM billing_dispute_event_outbox
                WHERE tenant_id=? AND status='succeeded'
                """,
                    (graph.tenant_id,),
                ).fetchone()[0]
            )
            == 2
        ),
        stage="recovery.all_outbox",
        timeout_seconds=40,
    )
    webhook_state = webhook.snapshot(str(first_delivery_id))
    require(
        webhook_state["network_deliveries"] == 4
        and webhook_state["logical_events"] == 2
        and webhook_state["duplicate_deliveries"] == 2
        and webhook_state["first_event_deliveries"] == 3
        and webhook_state["stable_delivery_id"] is True
        and webhook_state["invalid"] == 0
        and webhook_state["signature_failures"] == 0
        and webhook_state["forbidden_hits"] == 0
        and webhook_state["schema_failures"] == 0,
        "recovery.webhook",
        "counts",
    )
    database = _final_database_facts(
        graph,
        success_ids=success_ids,
        failure_id=failure_id,
        request_dispute_id=request_dispute_id,
        time_dispute_id=time_dispute_id,
    )
    provider_state = provider.snapshot()
    require(
        int(provider_state["calls"]) >= 5
        and int(provider_state["failures"]) >= 1
        and int(provider_state["invalid"]) == 0
        and int(provider_state["auth_mismatches"]) == 0,
        "provider.final",
        "counts",
    )
    return (
        {
            "services": {
                "control_http": True,
                "gateway_http": True,
                "provider_loopback": True,
                "webhook_loopback": True,
                "control_restarted": True,
            },
            "gateway": {
                "success_status": 200,
                "successful_requests": 4,
                "canonical_usage_rows": database["gateway"]["canonical_usage_rows"],
                "canonical_charge_rows": database["gateway"]["canonical_charge_rows"],
                "failure_status_class": failure_status // 100,
                "failed_request_rows": database["gateway"]["failed_request_rows"],
                "provider_network_calls": int(provider_state["calls"]),
            },
            "access_control": {
                "developer_status": 403,
                "cross_tenant_status": 404,
                "idempotency_conflict_status": 409,
                "live_revocation_status": 403,
                "error_contract_count": 4,
            },
            "disputes": database["disputes"],
            "recovery": {
                "initial_failure_committed": True,
                "stable_delivery_id": True,
                "receiver_deduplicated": True,
                "webhook_network_deliveries": webhook_state["network_deliveries"],
                "webhook_logical_events": webhook_state["logical_events"],
                "duplicate_deliveries": webhook_state["duplicate_deliveries"],
                "outbox_succeeded": database["recovery"]["outbox_succeeded"],
                "outbox_attempts": database["recovery"]["outbox_attempts"],
                "pending_leases": database["recovery"]["pending_leases"],
                "signatures_valid": True,
                "exact_payload_schema": True,
            },
            "database": database["database"],
        },
        tuple(storage_private),
        tuple(evidence_private),
    )


def execute_runtime(
    work: Path,
    graph: RuntimeGraph,
    runtime: RuntimeSecrets,
) -> tuple[dict[str, object], tuple[str, ...], tuple[str, ...]]:
    environment = service_environment(work, graph, runtime)
    provider = ProviderStub(
        port=PROVIDER_PORT,
        model="local-chat-model",
        key=runtime.provider_key,
        answer=runtime.response_canary,
        label="prd080",
    )
    webhook = WebhookStub(port=WEBHOOK_PORT)
    processes = OwnedProcesses()
    try:
        provider.start()
        webhook_url = webhook.start()
        result = _exercise_http_runtime(
            work,
            graph,
            environment,
            runtime,
            provider,
            webhook,
            webhook_url,
            processes,
        )
    except BaseException:
        try:
            _close_runtime_owners(processes, provider, webhook)
        except AcceptanceFailure:
            pass
        raise
    _close_runtime_owners(processes, provider, webhook)
    wait_ports_released()
    return result


def quiesce_database(graph: RuntimeGraph) -> None:
    connection = sqlite3.connect(graph.database)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
    finally:
        connection.close()
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{graph.database}{suffix}")
        if sidecar.exists():
            require(sidecar.parent == graph.database.parent, "cleanup.sidecar", "boundary")
            sidecar.unlink()


def scan_private_storage(work: Path, private_values: tuple[str, ...]) -> dict[str, int]:
    return _BASE.scan_private_storage(work, private_values)


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
    require(payload.get("prd_id") == "PRD-080", "evidence.shape", "prd")
    require(payload.get("status") == "passed", "evidence.shape", "status")
    require(payload.get("all_108_prds_complete") is False, "evidence.shape", "global_status")
    migration = payload.get("migration")
    require(
        isinstance(migration, dict) and set(migration) == {"fresh", "repeat", "down", "reup"},
        "evidence.shape",
        "migration",
    )
    for name, expected_count, expected_version in (
        ("fresh", 73, "0073"),
        ("repeat", 73, "0073"),
        ("down", 72, "0072"),
        ("reup", 73, "0073"),
    ):
        stage = migration.get(name)
        require(
            isinstance(stage, dict)
            and set(stage) == {"count", "version", "integrity", "foreign_key_violations"}
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
        name: {
            "count": count,
            "version": version,
            "integrity": "ok",
            "foreign_key_violations": 0,
        }
        for name, count, version in (
            ("fresh", 73, "0073"),
            ("repeat", 73, "0073"),
            ("down", 72, "0072"),
            ("reup", 73, "0073"),
        )
    }
    return {
        "schema_version": 1,
        "prd_id": "PRD-080",
        "status": "passed",
        "generated_at": utc_now(),
        "all_108_prds_complete": False,
        "migration": migration,
        "services": {
            "control_http": True,
            "gateway_http": True,
            "provider_loopback": True,
            "webhook_loopback": True,
            "control_restarted": True,
        },
        "gateway": {
            "success_status": 200,
            "successful_requests": 4,
            "canonical_usage_rows": 4,
            "canonical_charge_rows": 4,
            "failure_status_class": 5,
            "failed_request_rows": 1,
            "provider_network_calls": 5,
        },
        "access_control": {
            "developer_status": 403,
            "cross_tenant_status": 404,
            "idempotency_conflict_status": 409,
            "live_revocation_status": 403,
            "error_contract_count": 4,
        },
        "disputes": {
            "request_scope_items": 2,
            "time_range_items": 2,
            "approved_count": 1,
            "rejected_count": 1,
            "transition_count": 6,
            "released_hold_count": 4,
            "adjustment_count": 1,
            "reversal_count": 1,
            "partial_approval_exact": True,
        },
        "recovery": {
            "initial_failure_committed": True,
            "stable_delivery_id": True,
            "receiver_deduplicated": True,
            "webhook_network_deliveries": 4,
            "webhook_logical_events": 2,
            "duplicate_deliveries": 2,
            "outbox_succeeded": 2,
            "outbox_attempts": 2,
            "pending_leases": 0,
            "signatures_valid": True,
            "exact_payload_schema": True,
        },
        "database": {
            "integrity": "ok",
            "foreign_key_violations": 0,
            "audit_action_rows": 7,
            "event_rows": 2,
            "webhook_delivery_rows": 2,
        },
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
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
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
        facts, runtime_private, evidence_private = execute_runtime(work, graph, runtime)
        quiesce_database(graph)
        storage_private = runtime.values(runtime_private)
        publication_private = storage_private + evidence_private
        privacy = scan_private_storage(work, storage_private)
        require(source_provenance() == before, "evidence.source", "changed")
        wait_ports_released()
        remove_owned_work(work, owner)

        payload = empty_evidence_payload()
        payload["generated_at"] = utc_now()
        payload["migration"] = graph.migration
        for section in (
            "services",
            "gateway",
            "access_control",
            "disputes",
            "recovery",
            "database",
        ):
            payload[section] = facts[section]
        payload["privacy"] = privacy
        payload["cleanup"] = {
            "owned_processes_stopped": True,
            "fixed_ports_released": True,
            "sqlite_sidecars_remaining": int(privacy["sqlite_sidecars_remaining"]),
            "work_removed": True,
        }
        payload["source_provenance"] = before
        evidence = write_evidence(
            staging,
            final,
            payload,
            publication_private,
            owner,
        )
        scan_private_storage(final, publication_private)
        return evidence
    except BaseException:
        _cleanup_failed_run(work, staging, owner, graph)
        raise


def parse_arguments(argv: list[str] | None = None) -> Any:
    parser = SafeArgumentParser(description="Run PRD-080 runtime acceptance")
    parser.add_argument("--run-id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parse_arguments(argv)
        evidence = run(arguments.run_id)
    except AcceptanceFailure as exc:
        print(
            f"PRD-080 runtime acceptance failed: {exc.stage}:{exc.code}",
            file=sys.stderr,
        )
        return 1
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, RuntimeError, TypeError):
        print(
            "PRD-080 runtime acceptance failed: runtime:dependency_failure",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "PRD-080 runtime acceptance failed: runtime:unexpected_failure",
            file=sys.stderr,
        )
        return 1
    print(evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
