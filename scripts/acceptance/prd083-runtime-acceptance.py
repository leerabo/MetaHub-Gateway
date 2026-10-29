"""Real-process, privacy-bounded runtime acceptance for PRD-083.

The gate owns an empty SQLite database and starts four repository-local host
processes: Control, the independent MCP Scheduler, the isolated Sandbox Worker,
and a temporary HTTPS Streamable HTTP MCP server.  A fifth loopback receiver
validates the signed ``mcp.network_blocked`` delivery.  Network targets,
addresses, ACL entries, credentials, permits, arguments, and results are never
published; the complete temporary runtime tree is removed before evidence is
made formal.
"""

from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[2]
for _source_root in (
    ROOT / "shared-sdk" / "python",
    ROOT / "gateway-data-plane" / "src",
    ROOT / "control-plane-api" / "src",
    ROOT / "mcp-sandbox-worker" / "src",
):
    sys.path.insert(0, str(_source_root))

from metahub_shared.db import (  # noqa: E402
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError  # noqa: E402
from metahub_shared.runtime.webhooks import verify_webhook_signature  # noqa: E402


def _load_prd082() -> Any:
    name = "_metahub_prd082_helpers_for_prd083"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd082-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("runtime helper unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


LEGACY = _load_prd082()
PROCESS = LEGACY.PROCESS
AcceptanceFailure = LEGACY.AcceptanceFailure
SafeArgumentParser = LEGACY.SafeArgumentParser
OwnedProcesses = LEGACY.OwnedProcesses
require = LEGACY.require
utc_now = LEGACY.utc_now

LOOPBACK = "127.0.0.1"


def _runtime_private_host() -> str:
    candidates: set[str] = set()
    try:
        for answer in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
        ):
            candidates.add(str(answer[4][0]))
    except OSError:
        candidates = set()
    valid = sorted(
        value
        for value in candidates
        if (address := ipaddress.ip_address(value)).is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_unspecified
    )
    if not valid:
        raise RuntimeError("private runtime interface unavailable")
    return valid[0]


TARGET_HOST = _runtime_private_host()
CONTROL_PORT = 18930
SANDBOX_PORT = 18931
MCP_PORT = 18932
WEBHOOK_PORT = 18933
BLOCKED_PORT = 18934
FIXED_PORTS = (CONTROL_PORT, SANDBOX_PORT, MCP_PORT, WEBHOOK_PORT, BLOCKED_PORT)
WORK_ROOT = ROOT / ".tmp" / "prd083-runtime"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd083-runtime"
MIGRATIONS_DIR = ROOT / "infra" / "migrations"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
SCRIPT = Path(__file__).resolve()
OWNER_FILENAME = ".prd083-runtime-owner"
EXPECTED_MIGRATION_COUNT = 76
EXPECTED_MIGRATION_VERSION = "0076"
PREVIOUS_MIGRATION_VERSION = "0075"
SCHEDULER_POLL_INTERVAL_SECONDS = "2"
OPERATION_LEASE_SECONDS = "60"
INVOCATION_LEASE_SECONDS = "90"

SOURCE_FILES = {
    "runtime_acceptance": SCRIPT,
    "runtime_contract": ROOT
    / "scripts"
    / "acceptance"
    / "tests"
    / "test_prd083_runtime_acceptance.py",
    "runtime_helper_prd082": ROOT / "scripts" / "acceptance" / "prd082-runtime-acceptance.py",
    "migration_up": MIGRATIONS_DIR / "0076_mcp_sandbox_network_acl.sql",
    "migration_down": MIGRATIONS_DIR / "0076_mcp_sandbox_network_acl.down.sql",
    "scheduler_main": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "mcp_governance"
    / "scheduler_main.py",
    "scheduler": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "mcp_governance"
    / "scheduler.py",
    "sandbox_client": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "mcp_governance"
    / "sandbox_client.py",
    "sandbox_app": ROOT / "mcp-sandbox-worker" / "src" / "metahub_mcp_sandbox" / "app.py",
    "sandbox_acl": ROOT / "mcp-sandbox-worker" / "src" / "metahub_mcp_sandbox" / "acl.py",
    "sandbox_transport": ROOT
    / "mcp-sandbox-worker"
    / "src"
    / "metahub_mcp_sandbox"
    / "transport.py",
    "sandbox_config": ROOT
    / "shared-sdk"
    / "python"
    / "metahub_shared"
    / "runtime"
    / "mcp_sandbox_config.py",
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
        "workflow",
        "network",
        "traceability",
        "database",
        "privacy",
        "cleanup",
        "source_provenance",
    }
)
SERVICE_FACTS = {
    "control_http": True,
    "independent_scheduler": True,
    "independent_sandbox": True,
    "https_mcp": True,
    "local_webhook_receiver": True,
    "roles_isolated": True,
}
WORKFLOW_FACTS = {
    "waiting_policy_observed": True,
    "policy_activation_resumed": True,
    "discover_succeeded": True,
    "invoke_succeeded": True,
    "timeout_outcome_unknown": True,
}
NETWORK_FACTS = {
    "blocked_zero_call": True,
    "circuit_opened": True,
    "evt095_delivered": True,
    "permit_intent_committed": True,
    "fixed_ip_transport": True,
    "sessions_released": True,
}
DATABASE_FACTS = {
    "integrity": "ok",
    "foreign_key_violations": 0,
    "active_leases": 0,
    "nonterminal_rows": 0,
}
CLEANUP_FACTS = {
    "owned_processes_remaining": 0,
    "fixed_ports_released": True,
    "work_removed": True,
    "sandbox_active_sessions": 0,
    "sandbox_active_connections": 0,
}
FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "endpoint",
        "endpoint_url",
        "hostname",
        "ip",
        "address",
        "cidr",
        "authorization",
        "bearer_token",
        "token",
        "access_token",
        "provider_key",
        "password",
        "secret",
        "arguments",
        "parameters",
        "prompt",
        "response",
        "result",
        "content",
        "payload",
        "ciphertext",
        "database_url",
        "request_id",
        "trace_id",
        "lease_token",
        "permit",
    }
)
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$", re.ASCII)
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){1,3}$", re.ASCII)
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", re.ASCII)
_TERMINAL_INVOCATIONS = frozenset({"succeeded", "failed", "blocked", "rejected", "outcome_unknown"})
_OPERATION_STATES = frozenset({"queued", "processing", "succeeded", "failed"})
_SYNC_JOB_STATES = frozenset({"waiting_policy", "pending", "processing", "succeeded", "failed"})
_SANDBOX_FAILURE_CLASSES = frozenset(
    {
        "contract",
        "http",
        "network",
        "os",
        "runtime",
        "timeout",
        "dns",
        "connect",
        "tls",
        "headers",
        "body",
        "overall",
        "unexpected",
    }
)
_SANDBOX_ACTIONS = frozenset({"discover", "probe", "authorize", "call"})
_SANDBOX_FAILURE_DIAGNOSTICS = frozenset(
    f"{action}_{category}" for action in _SANDBOX_ACTIONS for category in _SANDBOX_FAILURE_CLASSES
)
_SCHEDULER_FAILURE_STAGES = frozenset(
    {
        "sync_prepare",
        "sync_finalize_success",
        "sync_finalize_failure",
        "sync_deadline_expired",
        "sync_lease_expired",
        "invoke_prepare",
        "invoke_probe_build",
        "invoke_probe_parse",
        "invoke_plan",
        "invoke_auth_build",
        "invoke_permit_parse",
        "invoke_intent",
        "invoke_probe_finalize",
        "invoke_call_finalize",
    }
)
_SCHEDULER_SQL_FAILURE_CLASSES = frozenset(
    {"invocation_transition", "decision_owner", "intent_owner", "attempt_owner", "other_sql"}
)
_SCHEDULER_FAILURE_DIAGNOSTICS = _SCHEDULER_FAILURE_STAGES | frozenset(
    f"invoke_{category}" for category in _SCHEDULER_SQL_FAILURE_CLASSES
)
_SCHEDULER_DISPATCH_PHASES = {
    "discover": "sync_discover",
    "probe": "invoke_probe",
    "authorize-call": "invoke_auth",
    "call": "invoke_call",
}
_SCHEDULER_DISPATCH_RESULTS = frozenset(
    {
        "ok",
        "network",
        "server",
        "sandbox",
        "outcome",
        "connect_timeout",
        "connect_error",
        "read_timeout",
        "write_timeout",
        "pool_timeout",
        "protocol",
        "network_error",
        "timeout_other",
        "http_error",
        "os_error",
        "runtime_error",
        "unexpected",
        "http_200",
        "http_4xx",
        "http_5xx",
        "http_other",
        "client_contract",
        "client_preflight",
        "invalid",
    }
)
_SCHEDULER_DISPATCH_DIAGNOSTICS = frozenset(
    f"{phase}_{result}"
    for phase in _SCHEDULER_DISPATCH_PHASES.values()
    for result in _SCHEDULER_DISPATCH_RESULTS
)
_SCHEDULER_FAILURE_DIAGNOSTICS |= _SCHEDULER_DISPATCH_DIAGNOSTICS
_WEBHOOK_FORBIDDEN_KEYS = frozenset(
    {
        "endpoint",
        "endpoint_url",
        "hostname",
        "ip",
        "address",
        "cidr",
        "authorization",
        "token",
        "secret",
        "permit",
        "arguments",
        "parameters",
        "prompt",
        "response",
        "result",
        "content",
    }
)


class RuntimeToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str
    value: str


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    legacy: Any = field(repr=False)
    sandbox_internal: str = field(repr=False)
    sandbox_envelope: str = field(repr=False)
    sandbox_permit: str = field(repr=False)
    sandbox_response: str = field(repr=False)
    argument_value: str = field(repr=False)
    result_value: str = field(repr=False)

    @classmethod
    def create(cls) -> RuntimeSecrets:
        def value(label: str) -> str:
            return f"Prd083-{label}-{secrets.token_urlsafe(48)}"

        return cls(
            legacy=LEGACY.RuntimeSecrets.create(),
            sandbox_internal=value("SandboxInternal"),
            sandbox_envelope=value("SandboxEnvelope"),
            sandbox_permit=value("SandboxPermit"),
            sandbox_response=value("SandboxResponse"),
            argument_value=value("Argument"),
            result_value=value("Result"),
        )

    def values(self, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        return (
            *self.legacy.values(),
            self.sandbox_internal,
            self.sandbox_envelope,
            self.sandbox_permit,
            self.sandbox_response,
            self.argument_value,
            self.result_value,
            *extra,
        )


@dataclass(frozen=True, slots=True)
class RuntimeGraph:
    database: Path = field(repr=False)
    database_url: str = field(repr=False)
    tenant_id: str
    admin_email: str
    admin_user_id: str
    migration: dict[str, dict[str, object]]


def _safe_run_id(value: str | None) -> str:
    candidate = (
        datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ-") + secrets.token_hex(4)
        if value is None
        else value
    )
    require(
        _SAFE_RUN_ID.fullmatch(candidate) is not None
        and ".." not in candidate
        and "/" not in candidate
        and "\\" not in candidate,
        "config.run_id",
        "invalid",
    )
    return candidate


def _direct_child(root: Path, name: str, *, stage: str) -> Path:
    require(Path(name).name == name and name not in {".", ".."}, stage, "name")
    resolved_root = root.resolve()
    result = (root / name).resolve()
    require(
        result.parent == resolved_root and resolved_root.is_relative_to(ROOT.resolve()),
        stage,
        "boundary",
    )
    return result


def _create_owned_tree(path: Path, root: Path, owner: str, *, stage: str) -> None:
    require(path.parent.resolve() == root.resolve() and not path.exists(), stage, "boundary")
    path.mkdir()
    claim = path / OWNER_FILENAME
    try:
        claim.write_text(owner + "\n", encoding="ascii", newline="\n")
    except Exception:
        if claim.exists():
            claim.unlink()
        path.rmdir()
        raise


def _remove_owned_tree(path: Path, root: Path, owner: str, *, stage: str) -> None:
    target = path.resolve()
    claim = target / OWNER_FILENAME
    require(
        target.parent == root.resolve()
        and target.is_dir()
        and not target.is_symlink()
        and claim.is_file()
        and not claim.is_symlink()
        and claim.read_text(encoding="ascii").strip() == owner,
        stage,
        "owner",
    )
    shutil.rmtree(target)
    require(not target.exists(), stage, "remaining")


def _create_directories(run_id: str, owner: str) -> tuple[Path, Path, Path]:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    require(not WORK_ROOT.is_symlink() and not ARTIFACT_ROOT.is_symlink(), "config.root", "symlink")
    work = _direct_child(WORK_ROOT, run_id, stage="config.work")
    staging = _direct_child(ARTIFACT_ROOT, f".{run_id}.staging", stage="config.staging")
    final = _direct_child(ARTIFACT_ROOT, run_id, stage="config.final")
    require(
        not work.exists() and not staging.exists() and not final.exists(), "config.run", "exists"
    )
    _create_owned_tree(work, WORK_ROOT, owner, stage="config.work")
    try:
        for name in ("logs", "temp", "spool", "tls"):
            (work / name).mkdir()
        _create_owned_tree(staging, ARTIFACT_ROOT, owner, stage="config.staging")
    except Exception:
        _remove_owned_tree(work, WORK_ROOT, owner, stage="cleanup.partial")
        raise
    return work, staging, final


@contextmanager
def _temporary_environment(values: Mapping[str, str]) -> Iterator[None]:
    prior = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(values)
        yield
    finally:
        os.environ.clear()
        os.environ.update(prior)


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _migration_health(database_url: str, count: int, version: str, stage: str) -> dict[str, object]:
    with closing(connect_database(database_url)) as connection:
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
    require(integrity == "ok" and foreign_keys == 0, stage, "database")
    return {
        "count": count,
        "version": version,
        "integrity": integrity,
        "foreign_key_violations": foreign_keys,
    }


def _prepare_database(work: Path, runtime: RuntimeSecrets) -> RuntimeGraph:
    database = (work / "prd083-runtime.sqlite3").resolve()
    require(
        database.parent == work.resolve() and not database.exists(), "database.path", "boundary"
    )
    database_url = _database_url(database)
    fresh = apply_migrations(database_url, migrations_dir=MIGRATIONS_DIR)
    require(
        len(fresh.applied) == EXPECTED_MIGRATION_COUNT
        and fresh.applied[-1:] == [EXPECTED_MIGRATION_VERSION],
        "migration.fresh",
        "result",
    )
    migration = {
        "fresh": _migration_health(
            database_url, EXPECTED_MIGRATION_COUNT, EXPECTED_MIGRATION_VERSION, "migration.fresh"
        )
    }
    repeated = apply_migrations(database_url, migrations_dir=MIGRATIONS_DIR)
    require(
        repeated.applied == [] and len(repeated.skipped) == EXPECTED_MIGRATION_COUNT,
        "migration.repeat",
        "result",
    )
    migration["repeat"] = _migration_health(
        database_url, EXPECTED_MIGRATION_COUNT, EXPECTED_MIGRATION_VERSION, "migration.repeat"
    )
    down = rollback_last_migration(
        database_url,
        migrations_dir=MIGRATIONS_DIR,
        expected_version=EXPECTED_MIGRATION_VERSION,
    )
    require(down.rolled_back == EXPECTED_MIGRATION_VERSION, "migration.down", "result")
    migration["down"] = _migration_health(
        database_url,
        EXPECTED_MIGRATION_COUNT - 1,
        PREVIOUS_MIGRATION_VERSION,
        "migration.down",
    )
    reup = apply_migrations(database_url, migrations_dir=MIGRATIONS_DIR)
    require(reup.applied == [EXPECTED_MIGRATION_VERSION], "migration.reup", "result")
    migration["reup"] = _migration_health(
        database_url, EXPECTED_MIGRATION_COUNT, EXPECTED_MIGRATION_VERSION, "migration.reup"
    )

    seed_environment = {
        "METAHUB_SEED_ADMIN_PASSWORD": runtime.legacy.admin_password,
        "METAHUB_SEED_VIRTUAL_KEY": runtime.legacy.virtual_key,
        "METAHUB_SEED_PROVIDER_API_KEY": runtime.legacy.provider_key,
        "METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
    }
    with _temporary_environment(seed_environment):
        seed = apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        admin = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? AND email=?",
            (seed.tenant_id, seed.admin_email),
        ).fetchone()
        require(admin is not None, "database.seed", "admin")
        require(
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            "database.seed",
            "integrity",
        )
        require(
            connection.execute("PRAGMA foreign_key_check").fetchall() == [],
            "database.seed",
            "foreign_keys",
        )
    return RuntimeGraph(
        database=database,
        database_url=database_url,
        tenant_id=seed.tenant_id,
        admin_email=seed.admin_email,
        admin_user_id=str(admin["user_id"]),
        migration=migration,
    )


def _base_environment(work: Path, graph: RuntimeGraph, runtime: RuntimeSecrets) -> dict[str, str]:
    environment = LEGACY._service_environment(work, graph, runtime.legacy, scheduler_enabled=False)
    environment.update(
        {
            "METAHUB_CONTROL_API_BASE_URL": f"http://{LOOPBACK}:{CONTROL_PORT}",
            "METAHUB_ALLOWED_ORIGINS": f"http://{LOOPBACK}:{CONTROL_PORT}",
            "METAHUB_MCP_ENABLED": "true",
            "METAHUB_MCP_ENCRYPTION_VERSION": "1",
            "METAHUB_MCP_ENCRYPTION_SECRET": runtime.legacy.mcp_encryption_secret,
            "METAHUB_MCP_HMAC_VERSION": "1",
            "METAHUB_MCP_HMAC_SECRET": runtime.legacy.mcp_hmac_secret,
            "METAHUB_MCP_TEST_LOOPBACK_ENABLED": "false",
            "METAHUB_MCP_SCHEDULER_POLL_INTERVAL_SECONDS": SCHEDULER_POLL_INTERVAL_SECONDS,
            "METAHUB_MCP_GLOBAL_CONCURRENCY": "1",
            "METAHUB_MCP_TENANT_CONCURRENCY": "1",
            "METAHUB_MCP_OPERATION_LEASE_SECONDS": OPERATION_LEASE_SECONDS,
            "METAHUB_MCP_INVOCATION_LEASE_SECONDS": INVOCATION_LEASE_SECONDS,
            "METAHUB_MCP_PROBE_DEADLINE_SECONDS": "10",
            "METAHUB_MCP_CALL_DEADLINE_SECONDS": "1",
            "METAHUB_MCP_NETWORK_CONNECT_TIMEOUT_MAX_SECONDS": "2",
            "METAHUB_MCP_NETWORK_PROBE_TIMEOUT_MAX_SECONDS": "10",
            "METAHUB_MCP_NETWORK_CALL_TIMEOUT_MAX_SECONDS": "5",
            "METAHUB_MCP_NETWORK_FAILURE_THRESHOLD_MAX": "1",
            "METAHUB_MCP_NETWORK_OPEN_SECONDS_MAX": "10",
        }
    )
    return environment


def role_environments(
    base: Mapping[str, str], runtime: RuntimeSecrets, work: Path
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    control = dict(base)
    control.update(
        {
            "METAHUB_MCP_EXECUTOR_MODE": "disabled",
            "METAHUB_MCP_SCHEDULER_ENABLED": "false",
        }
    )
    for key in tuple(control):
        if key.startswith("METAHUB_MCP_SANDBOX_"):
            control.pop(key)

    scheduler = dict(base)
    scheduler.update(
        {
            "METAHUB_MCP_EXECUTOR_MODE": "sandbox_http",
            "METAHUB_MCP_SCHEDULER_ENABLED": "true",
            "METAHUB_MCP_SANDBOX_BASE_URL": f"http://{LOOPBACK}:{SANDBOX_PORT}",
            "METAHUB_MCP_SANDBOX_INTERNAL_TOKEN": runtime.sandbox_internal,
            "METAHUB_MCP_SANDBOX_ENVELOPE_SECRET": runtime.sandbox_envelope,
            "METAHUB_MCP_SANDBOX_RESPONSE_KEY_VERSION": "1",
            "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT": runtime.sandbox_response,
            "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_PREVIOUS": "",
        }
    )
    scheduler.pop("METAHUB_MCP_SANDBOX_PERMIT_SECRET", None)
    scheduler.pop("METAHUB_MCP_SANDBOX_PRIVATE_CIDRS", None)

    sandbox = {key: value for key, value in base.items() if not key.startswith("METAHUB_")}
    sandbox["PYTHONPATH"] = os.pathsep.join(
        (
            str(ROOT / "shared-sdk" / "python"),
            str(ROOT / "mcp-sandbox-worker" / "src"),
        )
    )
    sandbox.update(
        {
            "TEMP": str(work / "temp"),
            "TMP": str(work / "temp"),
            "METAHUB_MCP_SANDBOX_WORKER_ENABLED": "true",
            "METAHUB_MCP_ENCRYPTION_VERSION": "1",
            "METAHUB_MCP_ENCRYPTION_SECRET": runtime.legacy.mcp_encryption_secret,
            "METAHUB_MCP_SANDBOX_INTERNAL_TOKEN": runtime.sandbox_internal,
            "METAHUB_MCP_SANDBOX_ENVELOPE_SECRET": runtime.sandbox_envelope,
            "METAHUB_MCP_SANDBOX_PERMIT_SECRET": runtime.sandbox_permit,
            "METAHUB_MCP_SANDBOX_RESPONSE_KEY_VERSION": "1",
            "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT": runtime.sandbox_response,
            "METAHUB_MCP_NETWORK_CONNECT_TIMEOUT_MAX_SECONDS": "2",
            "METAHUB_MCP_NETWORK_PROBE_TIMEOUT_MAX_SECONDS": "10",
            "METAHUB_MCP_NETWORK_CALL_TIMEOUT_MAX_SECONDS": "5",
            "METAHUB_MCP_NETWORK_FAILURE_THRESHOLD_MAX": "1",
            "METAHUB_MCP_NETWORK_OPEN_SECONDS_MAX": "10",
            "METAHUB_MCP_SANDBOX_PRIVATE_CIDRS": f"{TARGET_HOST}/32",
            "METAHUB_MCP_SANDBOX_DENY_CIDRS": "",
            "METAHUB_MCP_SANDBOX_ALLOWED_PORTS": str(MCP_PORT),
            "METAHUB_MCP_SANDBOX_DENY_PORTS": "",
            "METAHUB_MCP_SANDBOX_DNS_TIMEOUT_SECONDS": "2",
            "METAHUB_MCP_SANDBOX_TLS_TIMEOUT_SECONDS": "5",
            "METAHUB_MCP_SANDBOX_HEADERS_TIMEOUT_SECONDS": "5",
            "METAHUB_MCP_SANDBOX_BODY_IDLE_TIMEOUT_SECONDS": "5",
            "METAHUB_MCP_SANDBOX_MAX_DNS_ANSWERS": "4",
            "METAHUB_MCP_SANDBOX_MAX_ACTIVE_SESSIONS": "4",
            "METAHUB_MCP_SANDBOX_CLEANUP_TIMEOUT_SECONDS": "1",
        }
    )
    return control, scheduler, sandbox


def _assert_role_isolation(
    control: Mapping[str, str], scheduler: Mapping[str, str], sandbox: Mapping[str, str]
) -> None:
    require(control.get("METAHUB_MCP_EXECUTOR_MODE") == "disabled", "roles.control", "executor")
    require(
        not any(key.startswith("METAHUB_MCP_SANDBOX_") for key in control),
        "roles.control",
        "sandbox_secret",
    )
    require("METAHUB_MCP_SANDBOX_PERMIT_SECRET" not in scheduler, "roles.scheduler", "permit")
    require("METAHUB_MCP_SANDBOX_PRIVATE_CIDRS" not in scheduler, "roles.scheduler", "acl")
    require("METAHUB_DATABASE_URL" not in sandbox, "roles.sandbox", "database")
    require("METAHUB_MCP_HMAC_SECRET" not in sandbox, "roles.sandbox", "hmac")
    require("METAHUB_MCP_SANDBOX_PERMIT_SECRET" in sandbox, "roles.sandbox", "permit_missing")


def _ports_available() -> bool:
    probes: list[socket.socket] = []
    try:
        for port in FIXED_PORTS:
            host = TARGET_HOST if port in {MCP_PORT, BLOCKED_PORT} else LOOPBACK
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                candidate.bind((host, port))
            except OSError:
                candidate.close()
                return False
            probes.append(candidate)
        return True
    finally:
        for candidate in probes:
            candidate.close()


def _wait_port(process: Any, port: int, stage: str, *, host: str = LOOPBACK) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        require(process.process.poll() is None, stage, "process_exited")
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            if probe.connect_ex((host, port)) == 0:
                return
        finally:
            probe.close()
        time.sleep(0.1)
    raise AcceptanceFailure(stage, "timeout")


def _wait_ports_released() -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _ports_available():
            return
        time.sleep(0.1)
    raise AcceptanceFailure("cleanup.ports", "remaining")


def _generate_tls_material(work: Path) -> tuple[Path, Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    tls = (work / "tls").resolve()
    require(tls.parent == work.resolve(), "tls.path", "boundary")
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MetaHub runtime CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MetaHub runtime MCP")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(TARGET_HOST))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = tls / "ca.pem"
    cert_path = tls / "server.pem"
    key_path = tls / "server-key.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


def _json_object(response: httpx.Response, stage: str) -> dict[str, Any]:
    try:
        value = response.json()
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise AcceptanceFailure(stage, "json") from None
    require(type(value) is dict, stage, "shape")
    return value


def _assert_http(
    response: httpx.Response, status: int, stage: str, *, code: str | None = None
) -> dict[str, Any]:
    if response.status_code != status:
        failure = _json_object(response, stage).get("code")
        safe_failure = (
            failure
            if isinstance(failure, str) and _SAFE_ERROR_CODE.fullmatch(failure) is not None
            else "status"
        )
        raise AcceptanceFailure(stage, safe_failure)
    value = _json_object(response, stage)
    request_id = value.get("request_id")
    require(isinstance(request_id, str) and 1 <= len(request_id) <= 128, stage, "request_id")
    require("no-store" in response.headers.get("Cache-Control", "").casefold(), stage, "cache")
    if code is not None:
        require(value.get("code") == code, stage, "code")
    return value


def _headers(token: str, label: str, *, idempotency: bool = False) -> dict[str, str]:
    result = {"Authorization": f"Bearer {token}", "X-Request-ID": f"prd083-runtime-{label}"}
    if idempotency:
        result["Idempotency-Key"] = f"prd083-runtime-key-{label}"
    return result


def _login(client: httpx.Client, graph: RuntimeGraph, runtime: RuntimeSecrets) -> str:
    response = client.post(
        "/auth/login",
        headers={"X-Request-ID": "prd083-runtime-login"},
        json={
            "tenant_slug": "default",
            "email": graph.admin_email,
            "password": runtime.legacy.admin_password,
        },
    )
    require(response.status_code == 200, "http.login", "status")
    token = _json_object(response, "http.login").get("access_token")
    require(isinstance(token, str) and len(token) > 40, "http.login", "token")
    return token


def _wait_database(
    graph: RuntimeGraph, predicate: Any, stage: str, *, timeout_seconds: float = 45
) -> sqlite3.Row:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with closing(connect_database(graph.database_url)) as connection:
            row = predicate(connection)
            if row is not None:
                return row
        time.sleep(0.1)
    raise AcceptanceFailure(stage, "timeout")


def _wait_network_outbox(graph: RuntimeGraph, scheduler: Any) -> sqlite3.Row:
    try:
        return _wait_database(
            graph,
            lambda connection: connection.execute(
                "SELECT * FROM mcp_network_event_outbox WHERE tenant_id=? AND status='succeeded'",
                (graph.tenant_id,),
            ).fetchone(),
            "network.outbox",
        )
    except AcceptanceFailure as failure:
        with closing(connect_database(graph.database_url)) as connection:
            row = connection.execute(
                """
                SELECT status,attempt_count FROM mcp_network_event_outbox
                WHERE tenant_id=? ORDER BY created_at DESC,mcp_network_event_outbox_id DESC
                LIMIT 1
                """,
                (graph.tenant_id,),
            ).fetchone()
        status = str(row["status"]) if row is not None else "missing"
        if status == "pending" and row is not None:
            detail = "pending_unclaimed" if int(row["attempt_count"]) == 0 else "pending_retry"
        else:
            detail = status if status in {"processing", "succeeded", "failed"} else "missing"
        process = getattr(scheduler, "process", None)
        process_state = (
            "scheduler_alive"
            if process is not None and process.poll() is None
            else "scheduler_exited"
        )
        raise AcceptanceFailure("network.outbox", f"timeout.{detail}.{process_state}") from failure


def _wait_operation(graph: RuntimeGraph, operation_id: str, stage: str) -> sqlite3.Row:
    try:
        return _wait_database(
            graph,
            lambda connection: (
                row
                if (
                    row := connection.execute(
                        "SELECT * FROM mcp_server_operations WHERE mcp_server_operation_id=?",
                        (operation_id,),
                    ).fetchone()
                )
                is not None
                and row["status"] in {"succeeded", "failed"}
                else None
            ),
            stage,
        )
    except AcceptanceFailure as failure:
        with closing(connect_database(graph.database_url)) as connection:
            detail = _operation_timeout_detail(connection, operation_id)
        raise AcceptanceFailure(stage, detail) from failure


def _operation_timeout_detail(connection: sqlite3.Connection, operation_id: str) -> str:
    operation = connection.execute(
        "SELECT status FROM mcp_server_operations WHERE mcp_server_operation_id=?",
        (operation_id,),
    ).fetchone()
    job = connection.execute(
        "SELECT status FROM mcp_server_sync_jobs WHERE mcp_server_operation_id=?",
        (operation_id,),
    ).fetchone()
    operation_status = (
        str(operation["status"])
        if operation is not None and operation["status"] in _OPERATION_STATES
        else "missing"
    )
    job_status = (
        str(job["status"]) if job is not None and job["status"] in _SYNC_JOB_STATES else "missing"
    )
    detail = f"timeout.{operation_status}.{job_status}"
    return detail if _SAFE_ERROR_CODE.fullmatch(detail) is not None else "timeout.unknown"


def _wait_invocation(graph: RuntimeGraph, invocation_id: str, stage: str) -> sqlite3.Row:
    return _wait_database(
        graph,
        lambda connection: (
            row
            if (
                row := connection.execute(
                    "SELECT * FROM mcp_invocations WHERE mcp_invocation_id=?", (invocation_id,)
                ).fetchone()
            )
            is not None
            and row["status"] in _TERMINAL_INVOCATIONS
            else None
        ),
        stage,
        timeout_seconds=60,
    )


def _invocation_terminal_detail(row: sqlite3.Row, phase: str | None = None) -> str:
    status = str(row["status"])
    error_code = row["error_code"]
    if (
        status in _TERMINAL_INVOCATIONS
        and isinstance(error_code, str)
        and _SAFE_ERROR_CODE.fullmatch(error_code) is not None
    ):
        detail = f"{status}.{error_code}" + (f".{phase}" if phase is not None else "")
        if _SAFE_ERROR_CODE.fullmatch(detail) is not None:
            return detail
    return status if _SAFE_ERROR_CODE.fullmatch(status) is not None else "terminal.unknown"


def _invocation_failure_phase(graph: RuntimeGraph, invocation_id: str) -> str:
    with closing(connect_database(graph.database_url)) as connection:
        call_intent = connection.execute(
            "SELECT 1 FROM mcp_network_call_intents WHERE mcp_invocation_id=? LIMIT 1",
            (invocation_id,),
        ).fetchone()
        if call_intent is not None:
            return "call"
        decision = connection.execute(
            """
            SELECT phase FROM mcp_network_decisions
            WHERE mcp_invocation_id=?
            ORDER BY CASE phase WHEN 'call_preflight' THEN 0 ELSE 1 END,created_at DESC
            LIMIT 1
            """,
            (invocation_id,),
        ).fetchone()
    if decision is not None and decision["phase"] in {"probe", "call_preflight"}:
        return str(decision["phase"])
    return "prepare"


def _sandbox_exception_class(error: BaseException) -> str:
    pending = [error]
    visited: set[int] = set()
    observed: set[str] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        reason = getattr(current, "reason_code", None)
        fixed_reason = {
            "dns_unavailable": "dns",
            "connect_unavailable": "connect",
            "tls_unavailable": "tls",
            "headers_timeout": "headers",
            "body_timeout": "body",
            "overall_timeout": "overall",
        }.get(reason)
        if fixed_reason is not None:
            observed.add(fixed_reason)
        if isinstance(current, TimeoutError):
            observed.add("timeout")
        elif isinstance(current, httpx.HTTPError):
            observed.add("http")
        elif isinstance(current, OSError):
            observed.add("os")
        elif isinstance(current, (ValueError, TypeError)):
            observed.add("contract")
        elif isinstance(current, RuntimeError):
            observed.add("runtime")
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    for category in (
        "overall",
        "body",
        "headers",
        "tls",
        "connect",
        "dns",
        "timeout",
        "network",
        "http",
        "os",
        "contract",
        "runtime",
    ):
        if category in observed:
            return category
    return "unexpected"


def _scheduler_sqlite_failure_class(error: BaseException) -> str | None:
    markers = {
        "mcp_invocation_transition_invalid": "invocation_transition",
        "mcp_network_decision_owner_mismatch": "decision_owner",
        "mcp_network_call_intent_owner_mismatch": "intent_owner",
        "mcp_attempt_owner_mismatch": "attempt_owner",
    }
    pending = [error]
    visited: set[int] = set()
    observed_sql = False
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, sqlite3.Error):
            observed_sql = True
            message = str(current)
            for marker, category in markers.items():
                if marker in message:
                    return category
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "other_sql" if observed_sql else None


def _sync_finalize_timing_class(config: object, claim: object) -> str | None:
    database_url = getattr(config, "database_url", None)
    tenant_id = getattr(claim, "tenant_id", None)
    job_id = getattr(claim, "job_id", None)
    if not all(isinstance(value, str) and value for value in (database_url, tenant_id, job_id)):
        return None
    try:
        with closing(connect_database(database_url)) as connection:
            row = connection.execute(
                """
                SELECT unixepoch(deadline_at)<=unixepoch('now') AS deadline_expired,
                  unixepoch(lease_expires_at)<=unixepoch('now') AS lease_expired
                FROM mcp_server_sync_jobs
                WHERE tenant_id=? AND mcp_server_sync_job_id=?
                """,
                (tenant_id, job_id),
            ).fetchone()
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return None
    if row is None:
        return None
    if row["deadline_expired"] == 1:
        return "sync_deadline_expired"
    if row["lease_expired"] == 1:
        return "sync_lease_expired"
    return None


def _append_fixed_diagnostic(path: Path, marker: str, allowed: frozenset[str]) -> None:
    if marker not in allowed or path.is_symlink():
        return
    try:
        existing = path.read_text(encoding="ascii").splitlines() if path.exists() else []
        if any(value not in allowed for value in existing) or marker in existing:
            return
        with path.open("a", encoding="ascii", newline="\n") as stream:
            stream.write(marker + "\n")
    except (OSError, UnicodeError):
        return


def _read_fixed_diagnostics(path: Path, allowed: frozenset[str]) -> tuple[str, ...]:
    if not path.is_file() or path.is_symlink():
        return ()
    try:
        values = tuple(path.read_text(encoding="ascii").splitlines())
    except (OSError, UnicodeError):
        return ()
    return values if values and all(value in allowed for value in values) else ()


def _read_sandbox_failure_classes(work: Path) -> tuple[str, ...]:
    return _read_fixed_diagnostics(work / "sandbox-failure.txt", _SANDBOX_FAILURE_DIAGNOSTICS)


def _read_scheduler_failure_stages(work: Path) -> tuple[str, ...]:
    return _read_fixed_diagnostics(work / "scheduler-failure.txt", _SCHEDULER_FAILURE_DIAGNOSTICS)


def _scheduler_dispatch_marker(action: object, status: object, failure_code: object) -> str | None:
    phase = _SCHEDULER_DISPATCH_PHASES.get(action) if isinstance(action, str) else None
    if phase is None:
        return None
    if status == "succeeded" and failure_code is None:
        result = "ok"
    elif status == "blocked" and failure_code == "mcp.network_blocked":
        result = "network"
    elif status == "failed" and failure_code == "mcp.server_unavailable":
        result = "server"
    elif status == "failed" and failure_code == "mcp.sandbox_unavailable":
        result = "sandbox"
    elif status == "failed" and failure_code == "mcp.outcome_unknown":
        result = "outcome"
    else:
        result = "invalid"
    marker = f"{phase}_{result}"
    return marker if marker in _SCHEDULER_DISPATCH_DIAGNOSTICS else None


def _scheduler_transport_failure_class(failure: BaseException) -> str:
    if isinstance(failure, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(failure, httpx.ConnectError):
        return "connect_error"
    if isinstance(failure, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(failure, httpx.WriteTimeout):
        return "write_timeout"
    if isinstance(failure, httpx.PoolTimeout):
        return "pool_timeout"
    if isinstance(failure, (httpx.RemoteProtocolError, httpx.LocalProtocolError)):
        return "protocol"
    if isinstance(failure, httpx.NetworkError):
        return "network_error"
    if isinstance(failure, httpx.TimeoutException):
        return "timeout_other"
    if isinstance(failure, httpx.HTTPError):
        return "http_error"
    if isinstance(failure, OSError):
        return "os_error"
    if isinstance(failure, RuntimeError):
        return "runtime_error"
    return "unexpected"


def _scheduler_transport_status_class(status_code: object) -> str:
    if status_code == 200:
        return "http_200"
    if type(status_code) is int and 400 <= status_code < 500:
        return "http_4xx"
    if type(status_code) is int and 500 <= status_code < 600:
        return "http_5xx"
    return "http_other"


def _select_failure_diagnostic(
    failure: BaseException,
    scheduler: tuple[str, ...],
    sandbox: tuple[str, ...],
) -> str | None:
    stage = failure.stage if isinstance(failure, AcceptanceFailure) else ""
    if stage.startswith("http.") or stage.startswith("database.") or stage == "network.outbox":
        return None
    if stage == "workflow.discover" or stage.startswith("network.blocked"):
        scheduler_prefixes = ("sync_",)
        sandbox_prefixes = ("discover_",)
    elif stage.startswith("workflow.invoke") or stage in {"workflow.timeout", "network.circuit"}:
        scheduler_prefixes = ("invoke_",)
        sandbox_prefixes = ("probe_", "authorize_", "call_")
    else:
        scheduler_prefixes = ("sync_", "invoke_")
        sandbox_prefixes = tuple(f"{action}_" for action in _SANDBOX_ACTIONS)
    relevant_scheduler = tuple(value for value in scheduler if value.startswith(scheduler_prefixes))
    relevant_sandbox = tuple(value for value in sandbox if value.startswith(sandbox_prefixes))
    if relevant_sandbox and relevant_scheduler and relevant_scheduler[-1].endswith("_server"):
        return relevant_sandbox[-1]
    if relevant_scheduler:
        return relevant_scheduler[-1]
    return relevant_sandbox[-1] if relevant_sandbox else None


def _diagnostic_failure_code(code: str, diagnostic: str) -> str | None:
    appended = f"{code}.{diagnostic}"
    if _SAFE_ERROR_CODE.fullmatch(appended) is not None:
        return appended
    parts = code.split(".")
    replaced = ".".join((*parts[:3], diagnostic)) if len(parts) == 4 else ""
    return replaced if _SAFE_ERROR_CODE.fullmatch(replaced) is not None else None


def _register_server(
    client: httpx.Client, token: str, endpoint: str, label: str
) -> tuple[str, str]:
    response = client.post(
        "/mcp/servers",
        headers=_headers(token, f"register-{label}", idempotency=True),
        json={
            "name": f"PRD-083 runtime {label}",
            "endpoint_url": endpoint,
            "auth": {"type": "none"},
        },
    )
    value = _assert_http(response, 202, f"http.register_{label}")
    require(endpoint not in response.text, f"http.register_{label}", "endpoint_disclosed")
    server = value.get("server")
    operation = value.get("operation")
    require(type(server) is dict and type(operation) is dict, f"http.register_{label}", "shape")
    return str(server.get("mcp_server_id")), str(operation.get("mcp_server_operation_id"))


def _assert_waiting(graph: RuntimeGraph, server_id: str, stage: str) -> None:
    with closing(connect_database(graph.database_url)) as connection:
        row = connection.execute(
            "SELECT * FROM mcp_server_sync_jobs WHERE tenant_id=? AND mcp_server_id=?",
            (graph.tenant_id, server_id),
        ).fetchone()
    require(
        row is not None
        and row["status"] == "waiting_policy"
        and int(row["attempt_count"]) == 0
        and all(
            row[key] is None
            for key in (
                "lease_owner_id",
                "lease_token",
                "lease_expires_at",
                "next_attempt_at",
                "deadline_at",
                "started_at",
                "completed_at",
                "error_code",
            )
        ),
        stage,
        "contract",
    )


def _activate_network(client: httpx.Client, token: str, server_id: str, label: str) -> None:
    response = client.patch(
        f"/mcp/servers/{server_id}/network-policy",
        headers=_headers(token, f"network-{label}"),
        json={
            "expected_revision": 1,
            "status": "active",
            "egress_mode": "registered_endpoint_only",
            "connect_timeout_seconds": 1,
            "probe_timeout_seconds": 10,
            "call_timeout_seconds": 1,
            "failure_threshold": 1,
            "open_seconds": 10,
        },
    )
    if response.status_code != 200:
        failure = _json_object(response, f"http.network_{label}").get("code")
        safe_failure = (
            failure
            if isinstance(failure, str) and _SAFE_ERROR_CODE.fullmatch(failure) is not None
            else "status"
        )
        raise AcceptanceFailure(f"http.network_{label}", safe_failure)
    value = _assert_http(response, 200, f"http.network_{label}")
    policy = value.get("network_policy")
    require(
        type(policy) is dict and policy.get("revision") == 2 and policy.get("status") == "active",
        f"http.network_{label}",
        "policy",
    )


def _create_webhook(client: httpx.Client, token: str, endpoint: str) -> str:
    response = client.post(
        "/webhooks",
        headers=_headers(token, "webhook-create"),
        json={
            "name": "PRD-083 runtime receiver",
            "endpoint_url": endpoint,
            "event_types": ["mcp.network_blocked"],
            "status": "active",
            "timeout_ms": 2000,
        },
    )
    value = _assert_http(response, 200, "http.webhook_create")
    signing_secret = value.get("signing_secret")
    require(
        isinstance(signing_secret, str) and len(signing_secret) >= 32,
        "http.webhook_create",
        "secret",
    )
    return signing_secret


def _start_hidden_process(
    processes: OwnedProcesses,
    name: str,
    mode: str,
    *,
    work: Path,
    environment: dict[str, str],
    port: int,
    host: str = LOOPBACK,
) -> Any:
    process = processes.add(
        PROCESS.start_process(
            name,
            [str(PYTHON), str(SCRIPT), mode],
            work=work,
            environment=environment,
        )
    )
    _wait_port(process, port, f"health.{name}", host=host)
    return process


def _exercise_runtime(
    work: Path,
    graph: RuntimeGraph,
    runtime: RuntimeSecrets,
    processes: OwnedProcesses,
    control_environment: dict[str, str],
    scheduler_environment: dict[str, str],
    sandbox_environment: dict[str, str],
    ca_path: Path,
    cert_path: Path,
    key_path: Path,
    *,
    discovery_only: bool = False,
) -> tuple[dict[str, int], tuple[str, ...], dict[str, int]]:
    sandbox_environment.update(
        {
            "PRD083_RUNTIME_CA_FILE": str(ca_path),
            "PRD083_RUNTIME_SANDBOX_DIAGNOSTIC": str(work / "sandbox-failure.txt"),
        }
    )
    _start_hidden_process(
        processes,
        "sandbox",
        "--serve-sandbox",
        work=work,
        environment=sandbox_environment,
        port=SANDBOX_PORT,
    )
    mcp_counter = work / "mcp-counts.txt"
    mcp_environment = {
        key: value for key, value in control_environment.items() if not key.startswith("METAHUB_")
    }
    mcp_environment.update(
        {
            "PYTHONPATH": str(ROOT / "shared-sdk" / "python"),
            "PRD083_RUNTIME_CERT_FILE": str(cert_path),
            "PRD083_RUNTIME_KEY_FILE": str(key_path),
            "PRD083_RUNTIME_MCP_COUNTER": str(mcp_counter),
            "PRD083_RUNTIME_RESULT_VALUE": runtime.result_value,
        }
    )
    _start_hidden_process(
        processes,
        "https-mcp",
        "--serve-mcp",
        work=work,
        environment=mcp_environment,
        port=MCP_PORT,
        host=TARGET_HOST,
    )
    control = processes.add(
        PROCESS.start_service(
            "control",
            "metahub_control.app:app",
            CONTROL_PORT,
            work=work,
            environment=control_environment,
        )
    )
    PROCESS.wait_health(f"http://{LOOPBACK}:{CONTROL_PORT}/health", control, stage="health.control")

    private_runtime: list[str] = []
    endpoint = f"https://{TARGET_HOST}:{MCP_PORT}/mcp"
    blocked_endpoint = f"https://{TARGET_HOST}:{BLOCKED_PORT}/mcp"
    webhook_endpoint = f"http://{LOOPBACK}:{WEBHOOK_PORT}/events"
    private_runtime.extend(
        (endpoint, blocked_endpoint, webhook_endpoint, TARGET_HOST, f"{TARGET_HOST}/32")
    )
    webhook_counter = work / "webhook-counts.txt"

    with httpx.Client(
        base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
        timeout=httpx.Timeout(20, connect=3),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        admin = _login(client, graph, runtime)
        private_runtime.append(admin)
        signing_secret = _create_webhook(client, admin, webhook_endpoint)
        private_runtime.append(signing_secret)
        webhook_environment = {
            key: value
            for key, value in control_environment.items()
            if not key.startswith("METAHUB_")
        }
        webhook_environment.update(
            {
                "PYTHONPATH": str(ROOT / "shared-sdk" / "python"),
                "PRD083_RUNTIME_WEBHOOK_SECRET": signing_secret,
                "PRD083_RUNTIME_WEBHOOK_COUNTER": str(webhook_counter),
            }
        )
        _start_hidden_process(
            processes,
            "webhook-receiver",
            "--serve-webhook",
            work=work,
            environment=webhook_environment,
            port=WEBHOOK_PORT,
        )

        server_id, operation_id = _register_server(client, admin, endpoint, "allowed")
        _assert_waiting(graph, server_id, "workflow.waiting")
        scheduler_environment["PRD083_RUNTIME_SCHEDULER_DIAGNOSTIC"] = str(
            work / "scheduler-failure.txt"
        )
        scheduler = processes.add(
            PROCESS.start_process(
                "mcp-scheduler",
                [str(PYTHON), str(SCRIPT), "--serve-scheduler"],
                work=work,
                environment=scheduler_environment,
            )
        )
        time.sleep(0.4)
        require(scheduler.process.poll() is None, "health.scheduler", "process_exited")
        _assert_waiting(graph, server_id, "workflow.waiting_scheduler")
        _activate_network(client, admin, server_id, "allowed")
        operation = _wait_operation(graph, operation_id, "workflow.discover")
        discover_code = str(operation["error_code"] or operation["status"])
        require(
            operation["status"] == "succeeded" and operation["error_code"] is None,
            "workflow.discover",
            discover_code if _SAFE_ERROR_CODE.fullmatch(discover_code) is not None else "terminal",
        )
        if discovery_only:
            raise AcceptanceFailure("diagnostic.discovery", "passed")

        tools_response = client.get("/mcp/tools", headers=_headers(admin, "tools"))
        tools_value = _assert_http(tools_response, 200, "http.tools")
        tools = tools_value.get("tools")
        require(type(tools) is list and len(tools) == 1, "http.tools", "count")
        tool_id = str(tools[0].get("mcp_tool_id"))
        tool_policy = client.patch(
            f"/mcp/tools/{tool_id}",
            headers=_headers(admin, "tool-policy"),
            json={
                "expected_policy_revision": 1,
                "status": "active",
                "risk_level": "standard",
                "approval_required": False,
                "required_permission_codes": [],
                "allowed_role_ids": [],
                "allowed_user_ids": [graph.admin_user_id],
                "result_redaction_pointers": [],
            },
        )
        if tool_policy.status_code != 200:
            failure = _json_object(tool_policy, "http.tool_policy").get("code")
            safe_failure = (
                failure
                if isinstance(failure, str) and _SAFE_ERROR_CODE.fullmatch(failure) is not None
                else "status"
            )
            raise AcceptanceFailure("http.tool_policy", safe_failure)
        _assert_http(tool_policy, 200, "http.tool_policy")

        success = client.post(
            f"/mcp/tools/{tool_id}/invoke",
            headers=_headers(admin, "invoke-success", idempotency=True),
            json={"arguments": {"mode": "success", "payload": runtime.argument_value}},
        )
        success_value = _assert_http(success, 202, "http.invoke_success")
        require(
            runtime.argument_value not in success.text, "http.invoke_success", "argument_disclosed"
        )
        success_id = str(success_value.get("invocation", {}).get("mcp_invocation_id"))
        success_row = _wait_invocation(graph, success_id, "workflow.invoke_success")
        success_phase = _invocation_failure_phase(graph, success_id)
        require(
            success_row["status"] == "succeeded"
            and success_row["execution_status"] == "called"
            and int(success_row["tool_call_count"]) == 1,
            "workflow.invoke_success",
            _invocation_terminal_detail(success_row, success_phase),
        )

        timeout_argument = "Prd083-TimeoutArgument-" + secrets.token_urlsafe(40)
        private_runtime.append(timeout_argument)
        timeout = client.post(
            f"/mcp/tools/{tool_id}/invoke",
            headers=_headers(admin, "invoke-timeout", idempotency=True),
            json={"arguments": {"mode": "timeout", "payload": timeout_argument}},
        )
        timeout_value = _assert_http(timeout, 202, "http.invoke_timeout")
        timeout_id = str(timeout_value.get("invocation", {}).get("mcp_invocation_id"))
        timeout_row = _wait_invocation(graph, timeout_id, "workflow.timeout")
        require(
            timeout_row["status"] == "outcome_unknown"
            and timeout_row["error_code"] == "mcp.outcome_unknown"
            and timeout_row["execution_status"] == "unknown",
            "workflow.timeout",
            "terminal",
        )
        circuit = _wait_database(
            graph,
            lambda connection: connection.execute(
                """
                SELECT * FROM mcp_network_circuits
                WHERE tenant_id=? AND mcp_server_id=? AND state='open'
                """,
                (graph.tenant_id, server_id),
            ).fetchone(),
            "network.circuit",
        )
        require(
            int(circuit["failure_count"]) >= 1
            and circuit["last_failure_code"] == "overall_timeout",
            "network.circuit",
            "state",
        )

        blocked_server, blocked_operation = _register_server(
            client, admin, blocked_endpoint, "blocked"
        )
        _assert_waiting(graph, blocked_server, "workflow.blocked_waiting")
        before_calls = _counter_counts(mcp_counter)
        _activate_network(client, admin, blocked_server, "blocked")
        blocked_row = _wait_operation(graph, blocked_operation, "network.blocked")
        require(
            blocked_row["status"] == "failed"
            and blocked_row["error_code"] == "mcp.network_blocked",
            "network.blocked",
            "terminal",
        )
        after_calls = _counter_counts(mcp_counter)
        require(before_calls == after_calls, "network.blocked", "remote_call")

        _wait_network_outbox(graph, scheduler)
        deadline = time.monotonic() + 20
        while (
            time.monotonic() < deadline
            and _counter_counts(webhook_counter).get("mcp.network_blocked", 0) != 1
        ):
            time.sleep(0.1)
        require(
            _counter_counts(webhook_counter).get("mcp.network_blocked", 0) == 1,
            "network.webhook",
            "delivery",
        )

        with closing(connect_database(graph.database_url)) as connection:
            intent_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM mcp_network_call_intents WHERE tenant_id=?",
                    (graph.tenant_id,),
                ).fetchone()[0]
            )
            blocked_facts = connection.execute(
                """
                SELECT COUNT(*) AS events,
                  COUNT(DISTINCT mcp_network_decision_id) AS decisions
                FROM mcp_network_events WHERE tenant_id=?
                """,
                (graph.tenant_id,),
            ).fetchone()
        require(intent_count == 2, "network.permit_intent", "count")
        require(
            blocked_facts is not None and int(blocked_facts["events"]) == 1,
            "network.events",
            "count",
        )

    readiness = httpx.get(
        f"http://{LOOPBACK}:{SANDBOX_PORT}/health/ready",
        timeout=3,
        follow_redirects=False,
        trust_env=False,
    )
    ready = _json_object(readiness, "health.sandbox_final")
    require(
        readiness.status_code == 200
        and ready.get("active_sessions") == 0
        and ready.get("active_connections") == 0
        and ready.get("owned_tasks") == 0,
        "health.sandbox_final",
        "resources",
    )
    traceability = {
        "network_decision_count": _database_count(graph, "mcp_network_decisions"),
        "call_intent_count": _database_count(graph, "mcp_network_call_intents"),
        "network_event_count": _database_count(graph, "mcp_network_events"),
        "webhook_delivery_count": _database_count(
            graph, "webhook_deliveries", "event_type='mcp.network_blocked'"
        ),
        "remote_tool_call_count": sum(_counter_counts(mcp_counter).values()),
    }
    return (
        traceability,
        tuple(private_runtime),
        {
            "active_sessions": int(ready["active_sessions"]),
            "active_connections": int(ready["active_connections"]),
        },
    )


def _database_count(graph: RuntimeGraph, table: str, where: str = "1=1") -> int:
    allowed = {
        "mcp_network_decisions",
        "mcp_network_call_intents",
        "mcp_network_events",
        "webhook_deliveries",
    }
    require(
        table in allowed and where in {"1=1", "event_type='mcp.network_blocked'"},
        "database.count",
        "query",
    )
    with closing(connect_database(graph.database_url)) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0])


def _counter_counts(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    allowed = {"success", "timeout", "mcp.network_blocked"}
    result: dict[str, int] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        require(line in allowed, "runtime.counter", "value")
        result[line] = result.get(line, 0) + 1
    return result


def _final_database(graph: RuntimeGraph) -> tuple[dict[str, object], dict[str, int]]:
    with closing(connect_database(graph.database_url)) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        active_leases = 0
        for table in (
            "mcp_scheduler_tenants",
            "mcp_server_sync_jobs",
            "mcp_invocations",
            "mcp_network_event_outbox",
        ):
            active_leases += int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM {table}
                    WHERE lease_owner_id IS NOT NULL OR lease_token IS NOT NULL
                      OR lease_expires_at IS NOT NULL
                    """
                ).fetchone()[0]
            )
        nonterminal = (
            int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM mcp_server_sync_jobs
                    WHERE status IN('waiting_policy','pending','processing')
                    """
                ).fetchone()[0]
            )
            + int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM mcp_invocations
                    WHERE status IN('approval_pending','queued','probing','calling')
                    """
                ).fetchone()[0]
            )
            + int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM mcp_network_event_outbox
                    WHERE status IN('pending','processing')
                    """
                ).fetchone()[0]
            )
        )
        failed_tracking = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM mcp_invocations
                WHERE status IN('failed','blocked','outcome_unknown')
                """
            ).fetchone()[0]
        ) + int(
            connection.execute(
                "SELECT COUNT(*) FROM mcp_server_operations WHERE status='failed'"
            ).fetchone()[0]
        )
        cost_tracking = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM mcp_invocations
                WHERE cost_status IN('not_reported','unknown','not_applicable')
                """
            ).fetchone()[0]
        )
    database = {
        "integrity": integrity,
        "foreign_key_violations": foreign_keys,
        "active_leases": active_leases,
        "nonterminal_rows": nonterminal,
    }
    require(database == DATABASE_FACTS, "database.final", "facts")
    require(failed_tracking >= 2 and cost_tracking >= 2, "database.traceability", "counts")
    return database, {
        "failed_request_tracking_count": failed_tracking,
        "cost_status_tracking_count": cost_tracking,
    }


def _scan_logs(work: Path, private_values: tuple[str, ...]) -> dict[str, int]:
    logs = list((work / "logs").glob("*.log"))
    matches = 0
    for path in logs:
        text = path.read_text(encoding="utf-8", errors="replace")
        matches += sum(1 for value in private_values if value and value in text)
    require(matches == 0, "privacy.logs", "plaintext")
    return {
        "files_scanned": len(logs),
        "plaintext_matches": matches,
        "evidence_forbidden_key_matches": 0,
        "sqlite_sidecars_remaining": 0,
    }


def source_provenance() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in SOURCE_FILES.items():
        require(
            path.is_file() and path.resolve().is_relative_to(ROOT.resolve()),
            "source.provenance",
            "missing",
        )
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def assert_safe_evidence(value: object, private_values: tuple[str, ...]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    require(
        not any(item and item in encoded for item in private_values),
        "evidence.privacy",
        "plaintext",
    )

    def walk(current: object) -> None:
        if type(current) is dict:
            for key, child in current.items():
                require(
                    str(key).casefold() not in FORBIDDEN_EVIDENCE_KEYS,
                    "evidence.privacy",
                    "forbidden_key",
                )
                walk(child)
        elif type(current) is list:
            for child in current:
                walk(child)

    walk(value)


def empty_evidence_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "prd_id": "PRD-083",
        "status": "incomplete",
        "generated_at": None,
        "all_108_prds_complete": False,
        "migration": {},
        "services": {},
        "workflow": {},
        "network": {},
        "traceability": {},
        "database": {},
        "privacy": {},
        "cleanup": {},
        "source_provenance": {},
    }


def _validate_evidence(value: dict[str, object]) -> None:
    require(set(value) == EVIDENCE_TOP_LEVEL_KEYS, "evidence.shape", "top_level")
    require(
        value["schema_version"] == 1 and value["prd_id"] == "PRD-083", "evidence.shape", "identity"
    )
    require(
        value["status"] == "passed" and value["all_108_prds_complete"] is False,
        "evidence.shape",
        "status",
    )
    require(
        type(value["generated_at"]) is str and _UTC_TIMESTAMP.fullmatch(str(value["generated_at"])),
        "evidence.shape",
        "time",
    )
    migration = value["migration"]
    require(
        type(migration) is dict and set(migration) == {"fresh", "repeat", "down", "reup"},
        "evidence.shape",
        "migration",
    )
    expected_versions = {
        "fresh": (EXPECTED_MIGRATION_COUNT, EXPECTED_MIGRATION_VERSION),
        "repeat": (EXPECTED_MIGRATION_COUNT, EXPECTED_MIGRATION_VERSION),
        "down": (EXPECTED_MIGRATION_COUNT - 1, PREVIOUS_MIGRATION_VERSION),
        "reup": (EXPECTED_MIGRATION_COUNT, EXPECTED_MIGRATION_VERSION),
    }
    for key, (count, version) in expected_versions.items():
        require(
            migration[key]
            == {
                "count": count,
                "version": version,
                "integrity": "ok",
                "foreign_key_violations": 0,
            },
            "evidence.shape",
            f"migration_{key}",
        )
    require(value["services"] == SERVICE_FACTS, "evidence.shape", "services")
    require(value["workflow"] == WORKFLOW_FACTS, "evidence.shape", "workflow")
    require(value["network"] == NETWORK_FACTS, "evidence.shape", "network")
    traceability = value["traceability"]
    require(
        type(traceability) is dict
        and set(traceability)
        == {
            "network_decision_count",
            "call_intent_count",
            "network_event_count",
            "webhook_delivery_count",
            "remote_tool_call_count",
            "failed_request_tracking_count",
            "cost_status_tracking_count",
        }
        and all(type(item) is int and item >= 1 for item in traceability.values()),
        "evidence.shape",
        "traceability",
    )
    require(value["database"] == DATABASE_FACTS, "evidence.shape", "database")
    privacy = value["privacy"]
    require(
        type(privacy) is dict
        and set(privacy)
        == {
            "files_scanned",
            "plaintext_matches",
            "evidence_forbidden_key_matches",
            "sqlite_sidecars_remaining",
        }
        and type(privacy["files_scanned"]) is int
        and privacy["files_scanned"] >= 5
        and privacy["plaintext_matches"] == 0
        and privacy["evidence_forbidden_key_matches"] == 0
        and privacy["sqlite_sidecars_remaining"] == 0,
        "evidence.shape",
        "privacy",
    )
    require(value["cleanup"] == CLEANUP_FACTS, "evidence.shape", "cleanup")
    provenance = value["source_provenance"]
    require(
        type(provenance) is dict
        and set(provenance) == set(SOURCE_FILES)
        and all(
            type(item) is str and len(item) == 64 and set(item) <= set("0123456789abcdef")
            for item in provenance.values()
        ),
        "evidence.shape",
        "provenance",
    )


def _write_evidence(
    staging: Path,
    final: Path,
    owner: str,
    payload: dict[str, object],
    private_values: tuple[str, ...],
    expected_provenance: dict[str, str],
) -> Path:
    _validate_evidence(payload)
    assert_safe_evidence(payload, private_values)
    require(
        payload["source_provenance"] == expected_provenance == source_provenance(),
        "evidence.source",
        "changed",
    )
    evidence = staging / "runtime-acceptance.json"
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    with evidence.open("xb") as stream:
        stream.write(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    (staging / "SHA256SUMS.txt").write_text(
        f"{digest}  runtime-acceptance.json\n", encoding="ascii", newline="\n"
    )
    staging.replace(final)
    try:
        assert_safe_evidence(
            json.loads((final / evidence.name).read_text(encoding="utf-8")), private_values
        )
        require(source_provenance() == expected_provenance, "evidence.source", "changed")
        (final / OWNER_FILENAME).unlink()
        return final / evidence.name
    except BaseException:
        if final.exists():
            _remove_owned_tree(final, ARTIFACT_ROOT, owner, stage="cleanup.failed_final")
        raise


def run(run_id: str | None = None, *, discovery_only: bool = False) -> Path:
    actual_run_id = _safe_run_id(run_id)
    owner = hashlib.sha256(secrets.token_bytes(48)).hexdigest()
    runtime = RuntimeSecrets.create()
    require(PYTHON.is_file(), "runtime.python", "missing")
    require(_ports_available(), "config.ports", "unavailable")
    before = source_provenance()
    work, staging, final = _create_directories(actual_run_id, owner)
    graph: RuntimeGraph | None = None
    processes = OwnedProcesses()
    try:
        graph = _prepare_database(work, runtime)
        base = _base_environment(work, graph, runtime)
        control, scheduler, sandbox = role_environments(base, runtime, work)
        _assert_role_isolation(control, scheduler, sandbox)
        ca_path, cert_path, key_path = _generate_tls_material(work)
        traceability, runtime_private, sandbox_resources = _exercise_runtime(
            work,
            graph,
            runtime,
            processes,
            control,
            scheduler,
            sandbox,
            ca_path,
            cert_path,
            key_path,
            discovery_only=discovery_only,
        )
        processes.close()
        _wait_ports_released()
        database, database_tracking = _final_database(graph)
        private_values = runtime.values(runtime_private)
        privacy = _scan_logs(work, private_values)
        for suffix in ("-wal", "-shm", "-journal"):
            require(not Path(str(graph.database) + suffix).exists(), "cleanup.sqlite", "sidecar")
        require(source_provenance() == before, "evidence.source", "changed")
        _remove_owned_tree(work, WORK_ROOT, owner, stage="cleanup.work")
        payload = empty_evidence_payload()
        payload.update(
            {
                "status": "passed",
                "generated_at": utc_now(),
                "migration": graph.migration,
                "services": dict(SERVICE_FACTS),
                "workflow": dict(WORKFLOW_FACTS),
                "network": dict(NETWORK_FACTS),
                "traceability": {**traceability, **database_tracking},
                "database": database,
                "privacy": privacy,
                "cleanup": {
                    "owned_processes_remaining": len(processes.items),
                    "fixed_ports_released": True,
                    "work_removed": True,
                    "sandbox_active_sessions": sandbox_resources["active_sessions"],
                    "sandbox_active_connections": sandbox_resources["active_connections"],
                },
                "source_provenance": before,
            }
        )
        return _write_evidence(staging, final, owner, payload, private_values, before)
    except BaseException as failure:
        sandbox_failures = _read_sandbox_failure_classes(work)
        scheduler_failures = _read_scheduler_failure_stages(work)
        try:
            processes.close()
        except Exception:
            pass
        try:
            _wait_ports_released()
        except Exception:
            pass
        for path, root in ((final, ARTIFACT_ROOT), (staging, ARTIFACT_ROOT), (work, WORK_ROOT)):
            try:
                if path.exists():
                    _remove_owned_tree(path, root, owner, stage="cleanup.failed")
            except Exception:
                pass
        diagnostic = _select_failure_diagnostic(failure, scheduler_failures, sandbox_failures)
        if diagnostic is not None and isinstance(failure, AcceptanceFailure):
            detail = _diagnostic_failure_code(failure.code, diagnostic)
            if detail is not None:
                raise AcceptanceFailure(failure.stage, detail) from failure
        raise


def _path_from_environment(key: str) -> Path:
    raw = os.environ.get(key, "")
    path = Path(raw).resolve()
    require(
        raw != "" and path.is_file() and path.is_relative_to(WORK_ROOT.resolve()),
        "hidden.path",
        "invalid",
    )
    return path


def _serve_mcp() -> int:
    cert = _path_from_environment("PRD083_RUNTIME_CERT_FILE")
    key = _path_from_environment("PRD083_RUNTIME_KEY_FILE")
    counter_raw = os.environ.get("PRD083_RUNTIME_MCP_COUNTER", "")
    result_value = os.environ.get("PRD083_RUNTIME_RESULT_VALUE", "")
    counter = Path(counter_raw).resolve()
    require(
        counter_raw != ""
        and counter.parent.is_relative_to(WORK_ROOT.resolve())
        and result_value != "",
        "hidden.mcp",
        "environment",
    )
    import uvicorn
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import ToolAnnotations

    server = FastMCP(
        "MetaHub PRD-083 Runtime",
        host=TARGET_HOST,
        port=MCP_PORT,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        log_level="ERROR",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{TARGET_HOST}:{MCP_PORT}"],
            allowed_origins=[],
        ),
    )

    @server.tool(
        name="runtime_tool",
        description="Bounded PRD-083 runtime contract tool",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
        structured_output=True,
    )
    async def runtime_tool(mode: str, payload: str) -> RuntimeToolResult:
        """Return one bounded result or hold the call past its policy deadline."""

        del payload
        if mode not in {"success", "timeout"}:
            raise ValueError("unsupported mode")
        with counter.open("a", encoding="ascii", newline="\n") as stream:
            stream.write(mode + "\n")
        if mode == "timeout":
            import asyncio

            await asyncio.sleep(5)
        return RuntimeToolResult(mode=mode, value=result_value)

    uvicorn.run(
        server.streamable_http_app(),
        host=TARGET_HOST,
        port=MCP_PORT,
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
        access_log=False,
        log_level="error",
    )
    return 0


def _serve_sandbox() -> int:
    ca_path = _path_from_environment("PRD083_RUNTIME_CA_FILE")
    diagnostic_raw = os.environ.get("PRD083_RUNTIME_SANDBOX_DIAGNOSTIC", "")
    diagnostic = Path(diagnostic_raw).resolve()
    require(
        diagnostic_raw != ""
        and diagnostic.name == "sandbox-failure.txt"
        and diagnostic.parent.is_relative_to(WORK_ROOT.resolve())
        and not diagnostic.exists(),
        "hidden.sandbox",
        "diagnostic",
    )
    import certifi
    import uvicorn

    certifi.where = lambda: str(ca_path)  # type: ignore[method-assign]
    from metahub_mcp_sandbox import app as sandbox_app

    original_action = sandbox_app._mcp_action
    original_execute = sandbox_app._execute

    async def observed_execute(action: object, *args: Any, **kwargs: Any) -> Any:
        try:
            return await original_execute(action, *args, **kwargs)
        except BaseException as failure:
            safe_action = "authorize" if action == "authorize-call" else action
            marker = (
                f"{safe_action}_{_sandbox_exception_class(failure)}"
                if safe_action in _SANDBOX_ACTIONS
                else ""
            )
            _append_fixed_diagnostic(diagnostic, marker, _SANDBOX_FAILURE_DIAGNOSTICS)
            raise

    async def observed_action(*args: Any, **kwargs: Any) -> dict[str, object]:
        try:
            return await original_action(*args, **kwargs)
        except BaseException as failure:
            action = args[0] if args else None
            if action == "authorize-call":
                action = "authorize"
            marker = (
                f"{action}_{_sandbox_exception_class(failure)}"
                if action in _SANDBOX_ACTIONS
                else ""
            )
            _append_fixed_diagnostic(diagnostic, marker, _SANDBOX_FAILURE_DIAGNOSTICS)
            raise

    sandbox_app._mcp_action = observed_action
    sandbox_app._execute = observed_execute

    uvicorn.run(
        sandbox_app.create_sandbox_app(),
        host=LOOPBACK,
        port=SANDBOX_PORT,
        access_log=False,
        log_level="error",
    )
    return 0


def _serve_scheduler() -> int:
    diagnostic_raw = os.environ.get("PRD083_RUNTIME_SCHEDULER_DIAGNOSTIC", "")
    diagnostic = Path(diagnostic_raw).resolve()
    require(
        diagnostic_raw != ""
        and diagnostic.name == "scheduler-failure.txt"
        and diagnostic.parent.is_relative_to(WORK_ROOT.resolve())
        and not diagnostic.exists(),
        "hidden.scheduler",
        "diagnostic",
    )
    from metahub_control.mcp_governance import scheduler as scheduler_module

    observed = {
        "_prepare_sandbox_sync": "sync_prepare",
        "_finalize_sync_success": "sync_finalize_success",
        "_finalize_sync_failure": "sync_finalize_failure",
        "_prepare_sandbox_invocation": "invoke_prepare",
        "_sandbox_probe_dispatch": "invoke_probe_build",
        "parse_sandbox_probe": "invoke_probe_parse",
        "_prepare_sandbox_call_plan": "invoke_plan",
        "_sandbox_authorize_dispatch": "invoke_auth_build",
        "parse_sandbox_permit": "invoke_permit_parse",
        "_commit_sandbox_call_intent": "invoke_intent",
        "_finalize_probe_failure": "invoke_probe_finalize",
        "_finalize_call_result": "invoke_call_finalize",
        "_finalize_call_unknown": "invoke_call_finalize",
    }
    for name, stage in observed.items():
        original = getattr(scheduler_module, name)

        def wrapper(
            *args: Any, _original: Any = original, _stage: str = stage, **kwargs: Any
        ) -> Any:
            try:
                return _original(*args, **kwargs)
            except BaseException as failure:
                diagnostic_stage = _stage
                if _stage == "sync_finalize_success" and len(args) >= 2:
                    timing = _sync_finalize_timing_class(args[0], args[1])
                    if timing is not None:
                        diagnostic_stage = timing
                elif _stage == "invoke_intent":
                    category = _scheduler_sqlite_failure_class(failure)
                    if category is not None:
                        diagnostic_stage = f"invoke_{category}"
                _append_fixed_diagnostic(
                    diagnostic, diagnostic_stage, _SCHEDULER_FAILURE_DIAGNOSTICS
                )
                raise

        setattr(scheduler_module, name, wrapper)

    original_transport_request = httpx.HTTPTransport.handle_request
    active_dispatch_phase: str | None = None
    transport_result: str | None = None

    def observed_transport(transport: Any, request: Any) -> Any:
        nonlocal transport_result
        try:
            response = original_transport_request(transport, request)
        except BaseException as failure:
            transport_result = _scheduler_transport_failure_class(failure)
            if active_dispatch_phase is not None:
                _append_fixed_diagnostic(
                    diagnostic,
                    f"{active_dispatch_phase}_{transport_result}",
                    _SCHEDULER_FAILURE_DIAGNOSTICS,
                )
            raise
        transport_result = _scheduler_transport_status_class(getattr(response, "status_code", None))
        if active_dispatch_phase is not None:
            _append_fixed_diagnostic(
                diagnostic,
                f"{active_dispatch_phase}_{transport_result}",
                _SCHEDULER_FAILURE_DIAGNOSTICS,
            )
        return response

    httpx.HTTPTransport.handle_request = observed_transport
    original_dispatch = scheduler_module.McpSandboxHttpClient.dispatch

    def observed_dispatch(client: Any, request: Any) -> Any:
        nonlocal active_dispatch_phase, transport_result
        action = getattr(request, "action", None)
        phase = _SCHEDULER_DISPATCH_PHASES.get(action)
        active_dispatch_phase = phase
        transport_result = None
        try:
            response = original_dispatch(client, request)
        except BaseException:
            if phase is not None:
                result = (
                    "client_contract"
                    if transport_result == "http_200"
                    else "client_preflight"
                    if transport_result is None
                    else None
                )
                if result is not None:
                    _append_fixed_diagnostic(
                        diagnostic,
                        f"{phase}_{result}",
                        _SCHEDULER_FAILURE_DIAGNOSTICS,
                    )
            raise
        finally:
            active_dispatch_phase = None
        marker = _scheduler_dispatch_marker(
            action,
            getattr(response, "status", None),
            getattr(response, "failure_code", None),
        )
        if marker is not None:
            _append_fixed_diagnostic(diagnostic, marker, _SCHEDULER_FAILURE_DIAGNOSTICS)
        return response

    scheduler_module.McpSandboxHttpClient.dispatch = observed_dispatch

    import metahub_control.mcp_governance.scheduler_main as scheduler_main

    scheduler_main.main()
    return 0


def _contains_forbidden_webhook_key(value: object) -> bool:
    if type(value) is dict:
        return any(
            str(key).casefold() in _WEBHOOK_FORBIDDEN_KEYS or _contains_forbidden_webhook_key(child)
            for key, child in value.items()
        )
    if type(value) is list:
        return any(_contains_forbidden_webhook_key(child) for child in value)
    return False


def _serve_webhook() -> int:
    signing_secret = os.environ.get("PRD083_RUNTIME_WEBHOOK_SECRET", "")
    counter_raw = os.environ.get("PRD083_RUNTIME_WEBHOOK_COUNTER", "")
    counter = Path(counter_raw).resolve()
    require(
        len(signing_secret) >= 32
        and counter_raw != ""
        and counter.parent.is_relative_to(WORK_ROOT.resolve()),
        "hidden.webhook",
        "environment",
    )

    class Receiver(ThreadingHTTPServer):
        allow_reuse_address = False
        daemon_threads = True

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            valid = False
            try:
                lengths = self.headers.get_all("Content-Length", failobj=[])
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                    raise ValueError
                length = int(lengths[0])
                if not 0 < length <= 65_536:
                    raise ValueError
                body = self.rfile.read(length)
                value = json.loads(body.decode("utf-8", errors="strict"))
                valid = (
                    self.path == "/events"
                    and self.headers.get("X-MetaHub-Event") == "mcp.network_blocked"
                    and verify_webhook_signature(
                        self.headers.get("X-MetaHub-Signature", ""), signing_secret, body
                    )
                    and type(value) is dict
                    and value.get("event_type") == "mcp.network_blocked"
                    and type(value.get("payload")) is dict
                    and value["payload"].get("tool_call_count") == 0
                    and value["payload"].get("cost_status") == "not_applicable"
                    and not _contains_forbidden_webhook_key(value)
                )
                del body, value
            except (UnicodeError, ValueError, json.JSONDecodeError):
                valid = False
            if valid:
                with counter.open("a", encoding="ascii", newline="\n") as stream:
                    stream.write("mcp.network_blocked\n")
            self.send_response(204 if valid else 400)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    with Receiver((LOOPBACK, WEBHOOK_PORT), Handler) as server:
        server.serve_forever()
    return 0


def parse_arguments(argv: list[str] | None = None) -> Any:
    parser = SafeArgumentParser(description="Run PRD-083 runtime acceptance")
    parser.add_argument("--run-id")
    parser.add_argument("--serve-mcp", action="store_true", help=__import__("argparse").SUPPRESS)
    parser.add_argument(
        "--serve-sandbox", action="store_true", help=__import__("argparse").SUPPRESS
    )
    parser.add_argument(
        "--serve-scheduler", action="store_true", help=__import__("argparse").SUPPRESS
    )
    parser.add_argument(
        "--serve-webhook", action="store_true", help=__import__("argparse").SUPPRESS
    )
    parser.add_argument(
        "--diagnose-discovery", action="store_true", help=__import__("argparse").SUPPRESS
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parse_arguments(argv)
        if arguments.serve_mcp:
            return _serve_mcp()
        if arguments.serve_sandbox:
            return _serve_sandbox()
        if arguments.serve_scheduler:
            return _serve_scheduler()
        if arguments.serve_webhook:
            return _serve_webhook()
        evidence = run(arguments.run_id, discovery_only=arguments.diagnose_discovery)
    except AcceptanceFailure as exc:
        print(f"PRD-083 runtime acceptance failed: {exc.stage}:{exc.code}", file=sys.stderr)
        return 1
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, RuntimeError, TypeError):
        print("PRD-083 runtime acceptance failed: runtime:dependency_failure", file=sys.stderr)
        return 1
    except Exception:
        print("PRD-083 runtime acceptance failed: runtime:unexpected_failure", file=sys.stderr)
        return 1
    print(evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
