"""Repository-owned real runtime acceptance for PRD-082.

The runner creates one empty SQLite database, exercises the reversible 0075
migration, starts a real loopback Streamable HTTP MCP server and Control API,
and drives the production MCP scheduler against the official ``mcp`` SDK
adapter.  Dynamic endpoints, credentials, arguments, results, access tokens,
and approval step-up tokens remain memory-only or encrypted.  Published
evidence is restricted to fixed booleans, counts, migration versions, error
codes, and source hashes.
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
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[2]
for _source_root in (
    ROOT / "shared-sdk" / "python",
    ROOT / "gateway-data-plane" / "src",
    ROOT / "control-plane-api" / "src",
):
    sys.path.insert(0, str(_source_root))

from metahub_control.mcp_governance import executor as mcp_executor  # noqa: E402
from metahub_control.mcp_governance.scheduler import (  # noqa: E402
    run_mcp_outbox_once,
    run_mcp_scheduler_once,
)
from metahub_shared.db import (  # noqa: E402
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError  # noqa: E402
from metahub_shared.db.security import hash_password  # noqa: E402
from metahub_shared.runtime.config import AppConfig, load_app_config  # noqa: E402


def _load_helpers() -> Any:
    name = "_metahub_prd081_helpers_for_prd082"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd081-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("runtime helper unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


HELPERS = _load_helpers()
BASE = HELPERS.LEGACY
PROCESS = HELPERS.PROCESS
AcceptanceFailure = HELPERS.AcceptanceFailure
SafeArgumentParser = HELPERS.SafeArgumentParser
OwnedProcesses = HELPERS.OwnedProcesses
require = HELPERS.require
utc_now = HELPERS.utc_now

LOOPBACK = "127.0.0.1"
CONTROL_PORT = 18920
MCP_PORT = 18921
FIXED_PORTS = (CONTROL_PORT, MCP_PORT)
WORK_ROOT = ROOT / ".tmp" / "prd082-runtime"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd082-runtime"
MIGRATIONS_DIR = ROOT / "infra" / "migrations"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
SCRIPT = Path(__file__).resolve()
OWNER_FILENAME = ".prd082-runtime-owner"
EXPECTED_MIGRATION_COUNT = 75
EXPECTED_MIGRATION_VERSION = "0075"
PREVIOUS_MIGRATION_VERSION = "0074"

SOURCE_FILES = {
    "runtime_acceptance": SCRIPT,
    "runtime_contract": ROOT
    / "scripts"
    / "acceptance"
    / "tests"
    / "test_prd082_runtime_acceptance.py",
    "runtime_helper_prd081": ROOT / "scripts" / "acceptance" / "prd081-runtime-acceptance.py",
    "runtime_helper_prd080": ROOT / "scripts" / "acceptance" / "prd080-runtime-acceptance.py",
    "runtime_helper_prd079": ROOT / "scripts" / "acceptance" / "prd079-runtime-acceptance.py",
    "runtime_helper_prd075": ROOT / "scripts" / "acceptance" / "prd075-runtime-acceptance.py",
    "migration_up": MIGRATIONS_DIR / "0075_mcp_tool_registration_permissions.sql",
    "migration_down": MIGRATIONS_DIR / "0075_mcp_tool_registration_permissions.down.sql",
    "mcp_executor": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "mcp_governance"
    / "executor.py",
    "mcp_scheduler": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "mcp_governance"
    / "scheduler.py",
    "mcp_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "mcp_governance"
    / "service.py",
    "mcp_routes": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "mcp_governance"
    / "routes.py",
    "mcp_outbox": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "mcp_governance"
    / "outbox.py",
    "mcp_shared_package": ROOT / "shared-sdk" / "python" / "metahub_shared" / "mcp" / "__init__.py",
    "mcp_shared_contract": ROOT
    / "shared-sdk"
    / "python"
    / "metahub_shared"
    / "mcp"
    / "contracts.py",
    "mcp_shared_events": ROOT / "shared-sdk" / "python" / "metahub_shared" / "mcp" / "events.py",
    "mcp_shared_schema": ROOT / "shared-sdk" / "python" / "metahub_shared" / "mcp" / "schema.py",
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
        "transport",
        "api",
        "governance",
        "traceability",
        "database",
        "privacy",
        "cleanup",
        "source_provenance",
    }
)
SERVICE_FACTS = frozenset(
    {
        "control_http",
        "streamable_http_mcp",
        "official_sdk_loopback",
        "production_executor_zero_network",
    }
)
TRANSPORT_FACTS = frozenset(
    {
        "initialize_completed",
        "probe_completed",
        "request_bytes_tracked",
        "response_bytes_tracked",
        "tools_call_error_completed",
        "tools_call_success_completed",
        "tools_list_completed",
    }
)
API_FACTS = frozenset(
    {
        "backend_rbac_denied",
        "private_no_store",
        "real_control_http",
        "tenant_isolation_hidden",
        "uniform_request_id",
    }
)
GOVERNANCE_FACTS = {
    "clean_succeeded": True,
    "expired_call_recovered": True,
    "high_risk_approval_completed": True,
    "input_block_zero_call": True,
    "known_error_code": "mcp.tool_failed",
    "known_success": True,
    "log_only_succeeded": True,
    "non_self_approval": True,
    "output_blocked": True,
    "redaction_applied": True,
    "step_up_consumed": True,
    "unknown_error_code": "mcp.outcome_unknown",
}
TRACEABILITY_FACTS = {
    "approval_binding_count": 1,
    "attempt_count": 12,
    "audit_row_count": 7,
    "blocked_count": 2,
    "cost_status_tracking_count": 7,
    "event_count": 6,
    "failed_count": 1,
    "failed_request_tracking_count": 4,
    "invocation_count": 7,
    "outbox_terminal_count": 6,
    "outcome_unknown_count": 1,
    "remote_call_count": 8,
    "succeeded_count": 3,
}
DATABASE_FACTS = {
    "integrity": "ok",
    "foreign_key_violations": 0,
    "nonterminal_rows": 0,
    "active_leases": 0,
}
PRIVACY_FACTS = {
    "files_scanned": 9,
    "plaintext_matches": 0,
    "sqlite_sidecars_remaining": 0,
}
CLEANUP_FACTS = {
    "owned_processes_remaining": 0,
    "fixed_ports_released": True,
    "sqlite_sidecars_remaining": 0,
    "work_removed": True,
}
FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "endpoint",
        "endpoint_url",
        "authorization",
        "bearer_token",
        "token",
        "access_token",
        "step_up_token",
        "provider_key",
        "password",
        "secret",
        "arguments",
        "payload",
        "prompt",
        "response",
        "result",
        "content",
        "ciphertext",
        "database_url",
        "request_id",
        "trace_id",
        "lease_token",
    }
)
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$", re.ASCII)
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", re.ASCII)
_TERMINAL_INVOCATIONS = frozenset({"succeeded", "failed", "blocked", "rejected", "outcome_unknown"})
_CALL_MODES = frozenset(
    {
        "direct_success",
        "direct_error",
        "success",
        "log_only",
        "known_error",
        "output_block",
        "approved",
        "crash",
    }
)


class RuntimeMcpResult(BaseModel):
    """Explicit output contract for the real SDK server used by this gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str
    value: str


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
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
    mcp_encryption_secret: str = field(repr=False)
    mcp_hmac_secret: str = field(repr=False)
    bearer_token: str = field(repr=False)
    direct_argument: str = field(repr=False)
    result_canary: str = field(repr=False)
    output_block_canary: str = field(repr=False)

    @classmethod
    def create(cls) -> RuntimeSecrets:
        def value(label: str) -> str:
            return f"Prd082-{label}-{secrets.token_urlsafe(48)}"

        return cls(
            admin_password=value("AdminPassword-Aa1"),
            role_password=value("RolePassword-Aa1"),
            provider_key=value("ProviderCredential"),
            virtual_key=f"mhv_prd082_{secrets.token_urlsafe(44)}",
            auth_secret=value("AuthHmac"),
            provider_secret=value("ProviderEncryption"),
            prompt_secret=value("PromptEncryption"),
            file_secret=value("FileEncryption"),
            batch_secret=value("BatchEncryption"),
            cache_secret=value("CacheHmac"),
            approval_secret=value("ApprovalHmac"),
            internal_token=value("InternalConfig"),
            mcp_encryption_secret=value("McpEncryption"),
            mcp_hmac_secret=value("McpHmac"),
            bearer_token=value("McpBearer"),
            direct_argument=value("DirectArgument"),
            result_canary=value("RemoteResult"),
            output_block_canary=("prd082-output-block-prefix-" + secrets.token_urlsafe(48)),
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
            self.mcp_encryption_secret,
            self.mcp_hmac_secret,
            self.bearer_token,
            self.direct_argument,
            self.result_canary,
            self.output_block_canary,
            *extra,
        )


@dataclass(frozen=True, slots=True)
class RuntimeGraph:
    database: Path = field(repr=False)
    database_url: str = field(repr=False)
    tenant_id: str
    admin_email: str
    developer_email: str
    viewer_email: str
    security_email: str
    other_tenant_slug: str
    other_admin_email: str
    developer_role_id: str
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


def _valid_owner(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _direct_child(root: Path, name: str, *, stage: str) -> Path:
    require(Path(name).name == name and name not in {".", ".."}, stage, "name")
    resolved_root = root.resolve()
    resolved = (root / name).resolve()
    require(resolved.parent == resolved_root, stage, "boundary")
    require(resolved_root.is_relative_to(ROOT.resolve()), stage, "root")
    return resolved


def _create_directories(run_id: str, owner: str) -> tuple[Path, Path, Path]:
    require(_valid_owner(owner), "config.owner", "invalid")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    require(not WORK_ROOT.is_symlink() and not ARTIFACT_ROOT.is_symlink(), "config.root", "symlink")
    work = _direct_child(WORK_ROOT, run_id, stage="config.work")
    staging = _direct_child(ARTIFACT_ROOT, f".{run_id}.staging", stage="config.staging")
    final = _direct_child(ARTIFACT_ROOT, run_id, stage="config.artifact")
    require(
        not work.exists() and not staging.exists() and not final.exists(), "config.run", "exists"
    )
    claimed: list[tuple[Path, Path, str]] = []
    try:
        _create_owned_tree(work, WORK_ROOT, owner, stage="config.work_owner")
        claimed.append((work, WORK_ROOT, "cleanup.partial_work"))
        for name in ("files", "logs", "spool", "temp"):
            (work / name).mkdir()
        _create_owned_tree(staging, ARTIFACT_ROOT, owner, stage="config.staging_owner")
        claimed.append((staging, ARTIFACT_ROOT, "cleanup.partial_staging"))
    except Exception:
        for directory, root, stage in reversed(claimed):
            _remove_owned_tree(directory, root, owner, stage=stage)
        raise
    return work, staging, final


def _create_owned_tree(path: Path, root: Path, owner: str, *, stage: str) -> None:
    claim = path / OWNER_FILENAME
    require(path.parent.resolve() == root.resolve() and not path.exists(), stage, "boundary")
    try:
        path.mkdir()
        claim.write_text(f"{owner}\n", encoding="ascii", newline="\n")
        require(
            claim.is_file()
            and not claim.is_symlink()
            and claim.read_text(encoding="ascii").strip() == owner,
            stage,
            "owner",
        )
    except Exception:
        if path.is_dir() and not path.is_symlink():
            entries = list(path.iterdir())
            if not entries:
                path.rmdir()
            elif entries == [claim] and claim.is_file() and not claim.is_symlink():
                claim.unlink()
                path.rmdir()
        raise


def _remove_owned_tree(path: Path, root: Path, owner: str, *, stage: str) -> None:
    target = path.resolve()
    require(target.parent == root.resolve(), stage, "boundary")
    claim = target / OWNER_FILENAME
    require(
        target.is_dir()
        and not target.is_symlink()
        and claim.is_file()
        and not claim.is_symlink()
        and claim.read_text(encoding="ascii").strip() == owner,
        stage,
        "owner",
    )
    shutil.rmtree(target)
    require(not target.exists(), stage, "remaining")


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _migration_health(database_url: str, count: int, version: str, stage: str) -> dict[str, object]:
    with closing(connect_database(database_url)) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count,MAX(version) AS version "
            "FROM schema_migrations WHERE status='applied'"
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
    database = _direct_child(work, "prd082-runtime.sqlite3", stage="database.path")
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
        "METAHUB_SEED_ADMIN_PASSWORD": runtime.admin_password,
        "METAHUB_SEED_VIRTUAL_KEY": runtime.virtual_key,
        "METAHUB_SEED_PROVIDER_API_KEY": runtime.provider_key,
        "METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
    }
    with BASE.temporary_environment(seed_environment):
        seed = apply_seed_data(database_url)

    now = utc_now()
    developer_email = "developer-prd082@local.invalid"
    viewer_email = "viewer-prd082@local.invalid"
    security_email = "security-prd082@local.invalid"
    other_admin_email = "admin-prd082-other@local.invalid"
    other_tenant_id = "tenant_prd082_runtime_other"
    other_tenant_slug = "prd082-runtime-other"
    password_hash = hash_password(runtime.role_password)
    users = (
        (
            "user_prd082_runtime_developer",
            seed.tenant_id,
            developer_email,
            "PRD-082 Runtime Developer",
            password_hash,
            "developer",
            now,
            now,
        ),
        (
            "user_prd082_runtime_viewer",
            seed.tenant_id,
            viewer_email,
            "PRD-082 Runtime Viewer",
            password_hash,
            "viewer",
            now,
            now,
        ),
        (
            "user_prd082_runtime_security",
            seed.tenant_id,
            security_email,
            "PRD-082 Runtime Security",
            password_hash,
            "security_admin",
            now,
            now,
        ),
    )
    with closing(connect_database(database_url)) as connection:
        connection.executemany(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            users,
        )
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,?,'PRD-082 Runtime Other','internal_only','active',?,?)
            """,
            (other_tenant_id, other_tenant_slug, now, now),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES('user_prd082_runtime_other_admin',?,?,?,?,'admin','active',?,?)
            """,
            (other_tenant_id, other_admin_email, "PRD-082 Other Admin", password_hash, now, now),
        )
        developer_role = connection.execute(
            "SELECT role_id FROM roles WHERE tenant_id=? AND code='developer'",
            (seed.tenant_id,),
        ).fetchone()
        require(developer_role is not None, "database.seed", "developer_role")
        dlp_rules = (
            ("input-block", "prd082-input-block-prefix", "block", "input", 1),
            ("input-log", "prd082-input-log-prefix", "log_only", "input", 2),
            ("output-block", "prd082-output-block-prefix", "block", "output", 1),
        )
        for marker, pattern, action, scope, priority in dlp_rules:
            connection.execute(
                """
                INSERT INTO dlp_rules(
                  dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,action,
                  scan_scope,status,priority,description,created_by,updated_by,
                  created_at,updated_at
                ) VALUES(?,?,?,'keyword',?,?,?,?,'active',?,'',?,?,?,?)
                """,
                (
                    "dlpr_" + secrets.token_hex(16),
                    seed.tenant_id,
                    f"PRD-082 {marker}",
                    pattern,
                    "sha256:" + hashlib.sha256(f"keyword:{pattern}".encode()).hexdigest(),
                    action,
                    scope,
                    priority,
                    "user_prd082_runtime_developer",
                    "user_prd082_runtime_developer",
                    now,
                    now,
                ),
            )
        connection.commit()
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
        developer_email=developer_email,
        viewer_email=viewer_email,
        security_email=security_email,
        other_tenant_slug=other_tenant_slug,
        other_admin_email=other_admin_email,
        developer_role_id=str(developer_role["role_id"]),
        migration=migration,
    )


def _service_environment(
    work: Path,
    graph: RuntimeGraph,
    runtime: RuntimeSecrets,
    *,
    scheduler_enabled: bool,
) -> dict[str, str]:
    environment = BASE.service_environment(work, graph, runtime)
    environment.update(
        {
            "METAHUB_CONTROL_API_BASE_URL": f"http://{LOOPBACK}:{CONTROL_PORT}",
            "METAHUB_ALLOWED_ORIGINS": f"http://{LOOPBACK}:{CONTROL_PORT}",
            "METAHUB_MCP_ENABLED": "true",
            "METAHUB_MCP_ENCRYPTION_VERSION": "1",
            "METAHUB_MCP_ENCRYPTION_SECRET": runtime.mcp_encryption_secret,
            "METAHUB_MCP_HMAC_VERSION": "1",
            "METAHUB_MCP_HMAC_SECRET": runtime.mcp_hmac_secret,
            "METAHUB_MCP_EXECUTOR_MODE": "test_loopback",
            "METAHUB_MCP_TEST_LOOPBACK_ENABLED": "true",
            "METAHUB_MCP_SCHEDULER_ENABLED": "true" if scheduler_enabled else "false",
            "METAHUB_MCP_SCHEDULER_POLL_INTERVAL_SECONDS": "0.1",
            "METAHUB_MCP_OPERATION_LEASE_SECONDS": "30",
            "METAHUB_MCP_INVOCATION_LEASE_SECONDS": "30",
            "METAHUB_MCP_PROBE_DEADLINE_SECONDS": "10",
            "METAHUB_MCP_CALL_DEADLINE_SECONDS": "5",
            "METAHUB_APPROVAL_OUTBOX_ENABLED": "true",
            "METAHUB_APPROVAL_WRITEBACK_ENABLED": "true",
            "PRD082_MCP_COUNTER_FILE": str(work / "mcp-call-counts.txt"),
            "PRD082_MCP_RESULT_VALUE": runtime.result_canary,
            "PRD082_MCP_OUTPUT_BLOCK_VALUE": runtime.output_block_canary,
            "PRD082_MCP_PORT": str(MCP_PORT),
        }
    )
    return environment


@contextmanager
def _isolated_process_environment(values: dict[str, str]) -> Iterator[None]:
    previous = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(values)
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _load_scheduler_config(environment: dict[str, str]) -> AppConfig:
    values = dict(environment)
    values["METAHUB_MCP_SCHEDULER_ENABLED"] = "true"
    with _isolated_process_environment(values):
        return load_app_config("control-plane-api")


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


def _assert_ports_available() -> None:
    require(_ports_available(), "config.ports", "fixed_port_unavailable")


def _wait_port(process: Any, port: int, stage: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        require(process.process.poll() is None, stage, "process_exited")
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            if probe.connect_ex((LOOPBACK, port)) == 0:
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


def _json_object(response: httpx.Response, stage: str) -> dict[str, Any]:
    try:
        value = response.json()
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise AcceptanceFailure(stage, "json") from None
    require(type(value) is dict, stage, "shape")
    return value


def _assert_http_contract(
    response: httpx.Response,
    expected_status: int,
    stage: str,
    *,
    code: str | None = None,
) -> dict[str, Any]:
    require(response.status_code == expected_status, stage, "status")
    value = _json_object(response, stage)
    request_id = value.get("request_id")
    response_request_id = response.headers.get("X-Request-ID")
    require(
        isinstance(request_id, str)
        and 1 <= len(request_id) <= 128
        and (response_request_id is None or response_request_id == request_id),
        stage,
        "request_id",
    )
    require("no-store" in response.headers.get("Cache-Control", "").casefold(), stage, "cache")
    if code is not None:
        require(value.get("code") == code, stage, "code")
    return value


def _login(client: httpx.Client, slug: str, email: str, password: str, label: str) -> str:
    response = client.post(
        "/auth/login",
        headers={"X-Request-ID": f"prd082-runtime-login-{label}"},
        json={"tenant_slug": slug, "email": email, "password": password},
    )
    require(response.status_code == 200, f"http.login_{label}", "status")
    token = _json_object(response, f"http.login_{label}").get("access_token")
    require(isinstance(token, str) and len(token) > 40, f"http.login_{label}", "token")
    return token


def _headers(token: str, label: str, *, idempotency: bool = False) -> dict[str, str]:
    result = {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": f"prd082-runtime-{label}",
    }
    if idempotency:
        result["Idempotency-Key"] = f"prd082-runtime-key-{label}"
    return result


def _require_response_private(
    response: httpx.Response, values: tuple[str, ...], stage: str
) -> None:
    require(not any(value and value in response.text for value in values), stage, "private")


def _wait_database(
    graph: RuntimeGraph,
    predicate: Any,
    stage: str,
    *,
    timeout_seconds: float = 20,
) -> sqlite3.Row:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with closing(connect_database(graph.database_url)) as connection:
            row = predicate(connection)
            if row is not None:
                return row
        time.sleep(0.1)
    raise AcceptanceFailure(stage, "timeout")


def _run_until_operation_terminal(
    config: AppConfig, graph: RuntimeGraph, operation_id: str
) -> sqlite3.Row:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with closing(connect_database(graph.database_url)) as connection:
            row = connection.execute(
                "SELECT * FROM mcp_server_operations WHERE mcp_server_operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is not None and row["status"] in {"succeeded", "failed"}:
                return row
        run_mcp_scheduler_once(config, worker_id="prd082-runtime-operation")
        time.sleep(0.05)
    raise AcceptanceFailure("scheduler.operation", "timeout")


def _run_until_invocation_terminal(
    config: AppConfig, graph: RuntimeGraph, invocation_id: str
) -> sqlite3.Row:
    # A transient SQLite writer can strand the fairness lease until its 30-second takeover.
    # Include that complete recovery cycle plus a fresh worker attempt.
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        with closing(connect_database(graph.database_url)) as connection:
            row = connection.execute(
                "SELECT * FROM mcp_invocations WHERE mcp_invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if row is not None and row["status"] in _TERMINAL_INVOCATIONS:
                return row
        run_mcp_scheduler_once(config, worker_id="prd082-runtime-invocation")
        time.sleep(0.05)
    raise AcceptanceFailure("scheduler.invocation", "timeout")


def _drain_mcp_outbox(config: AppConfig, graph: RuntimeGraph) -> None:
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        progressed = run_mcp_outbox_once(config, worker_id="prd082-runtime-outbox")
        with closing(connect_database(graph.database_url)) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS pending,
                  coalesce(sum(CASE WHEN status='processing'
                    AND unixepoch(lease_expires_at)>unixepoch('now') THEN 1 ELSE 0 END),0)
                    AS active
                FROM mcp_invocation_event_outbox
                WHERE status IN('pending','processing')
                """
            ).fetchone()
            require(row is not None, "outbox.drain", "state")
            pending = int(row["pending"])
            active = int(row["active"])
        if pending == 0:
            return
        if progressed:
            continue
        require(active > 0, "outbox.drain", "stalled")
        time.sleep(0.05)
    raise AcceptanceFailure("outbox.drain", "timeout")


def _call_counts(path: Path) -> dict[str, int]:
    counts = {mode: 0 for mode in _CALL_MODES}
    if not path.exists():
        return counts
    require(path.is_file() and not path.is_symlink(), "mcp.counter", "type")
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        raise AcceptanceFailure("mcp.counter", "read") from None
    for line in lines:
        require(line in _CALL_MODES, "mcp.counter", "value")
        counts[line] += 1
    return counts


def _production_executor_zero_network(config: AppConfig) -> bool:
    factory_called = False

    def forbidden_factory(*_args: object, **_kwargs: object) -> Any:
        nonlocal factory_called
        factory_called = True
        raise AssertionError("factory must not run")

    production = config.model_copy(update={"environment": "prod"})
    executor = mcp_executor.McpExecutor(production, session_factory=forbidden_factory)
    original_getaddrinfo = socket.getaddrinfo
    original_socket = socket.socket

    def forbidden_network(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("network must not run")

    try:
        socket.getaddrinfo = forbidden_network  # type: ignore[assignment]
        socket.socket = forbidden_network  # type: ignore[assignment,misc]
        try:
            executor.discover(
                mcp_executor.McpDiscoverRequest(
                    mcp_executor.McpConnectionMaterial(
                        endpoint=f"http://{LOOPBACK}:{MCP_PORT}/mcp",
                        bearer_token=None,
                    )
                )
            )
        except mcp_executor.McpExecutorError as exc:
            return exc.code == "mcp.executor_disabled" and not factory_called
        return False
    finally:
        socket.getaddrinfo = original_getaddrinfo  # type: ignore[assignment]
        socket.socket = original_socket  # type: ignore[assignment,misc]


def _exercise_official_sdk(
    config: AppConfig, endpoint: str, runtime: RuntimeSecrets
) -> dict[str, object]:
    connection = mcp_executor.McpConnectionMaterial(
        endpoint=endpoint,
        bearer_token=runtime.bearer_token,
    )
    executor = mcp_executor.McpExecutor(config)
    discovered = executor.discover(mcp_executor.McpDiscoverRequest(connection))
    require(
        discovered.protocol_version.startswith("20")
        and [tool.name for tool in discovered.tools] == ["governed_tool"]
        and discovered.request_bytes > 0
        and discovered.response_bytes > 0,
        "sdk.discover",
        "contract",
    )
    probe = executor.probe(mcp_executor.McpProbeRequest(connection, "governed_tool"))
    require(
        probe.tool.name == "governed_tool" and probe.request_bytes > 0 and probe.response_bytes > 0,
        "sdk.probe",
        "contract",
    )
    success = executor.call(
        mcp_executor.McpCallRequest(
            connection,
            "governed_tool",
            {"mode": "direct_success", "payload": runtime.direct_argument},
        )
    )
    require(
        success.is_error is False
        and success.structured_content is not None
        and success.structured_content.get("value") == runtime.result_canary
        and success.request_bytes > 0
        and success.response_bytes > 0,
        "sdk.call_success",
        "contract",
    )
    known_error = executor.call(
        mcp_executor.McpCallRequest(
            connection,
            "governed_tool",
            {"mode": "direct_error", "payload": runtime.direct_argument},
        )
    )
    require(known_error.is_error is True, "sdk.call_error", "contract")
    return {
        "initialize_completed": True,
        "tools_list_completed": True,
        "probe_completed": True,
        "tools_call_success_completed": True,
        "tools_call_error_completed": True,
        "request_bytes_tracked": True,
        "response_bytes_tracked": True,
    }


def _invoke(
    client: httpx.Client,
    token: str,
    tool_id: str,
    label: str,
    argument: str,
) -> tuple[httpx.Response, dict[str, Any]]:
    response = client.post(
        f"/mcp/tools/{tool_id}/invoke",
        headers=_headers(token, label, idempotency=True),
        json={"arguments": {"mode": label, "payload": argument}},
    )
    value = _assert_http_contract(response, 202, f"http.invoke_{label}")
    _require_response_private(response, (argument,), f"http.invoke_{label}")
    return response, value


def _exercise_http_runtime(
    work: Path,
    graph: RuntimeGraph,
    runtime: RuntimeSecrets,
    environment: dict[str, str],
    config: AppConfig,
    processes: OwnedProcesses,
    endpoint: str,
) -> tuple[dict[str, object], tuple[str, ...]]:
    control = processes.add(
        PROCESS.start_service(
            "control",
            "metahub_control.app:app",
            CONTROL_PORT,
            work=work,
            environment=environment,
        )
    )
    PROCESS.wait_health(f"http://{LOOPBACK}:{CONTROL_PORT}/health", control, stage="health.control")
    counter = work / "mcp-call-counts.txt"
    private_runtime: list[str] = []
    arguments: dict[str, str] = {
        "success": "Prd082-CleanArgument-" + secrets.token_urlsafe(32),
        "log_only": "prd082-input-log-prefix-" + secrets.token_urlsafe(32),
        "known_error": "Prd082-ErrorArgument-" + secrets.token_urlsafe(32),
        "output_block": "Prd082-OutputBlockArgument-" + secrets.token_urlsafe(32),
        "crash": "Prd082-CrashArgument-" + secrets.token_urlsafe(32),
        "approved": "Prd082-ApprovedArgument-" + secrets.token_urlsafe(32),
        "blocked": "prd082-input-block-prefix-" + secrets.token_urlsafe(32),
    }
    private_runtime.extend(arguments.values())

    with httpx.Client(
        base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
        timeout=httpx.Timeout(20, connect=3),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        anonymous = client.get("/mcp/tools", headers={"X-Request-ID": "prd082-runtime-anonymous"})
        _assert_http_contract(anonymous, 401, "http.anonymous", code="auth.token_missing")
        admin = _login(client, "default", graph.admin_email, runtime.admin_password, "admin")
        developer = _login(
            client, "default", graph.developer_email, runtime.role_password, "developer"
        )
        viewer = _login(client, "default", graph.viewer_email, runtime.role_password, "viewer")
        security = _login(
            client, "default", graph.security_email, runtime.role_password, "security"
        )
        other = _login(
            client,
            graph.other_tenant_slug,
            graph.other_admin_email,
            runtime.role_password,
            "other",
        )
        private_runtime.extend((admin, developer, viewer, security, other))

        viewer_denied = client.post(
            "/mcp/servers",
            headers=_headers(viewer, "viewer-denied", idempotency=True),
            json={
                "name": "denied",
                "endpoint_url": endpoint,
                "auth": {"type": "bearer", "bearer_token": runtime.bearer_token},
            },
        )
        _assert_http_contract(
            viewer_denied,
            403,
            "http.viewer_denied",
            code="auth.permission_denied",
        )
        _require_response_private(
            viewer_denied,
            (endpoint, runtime.bearer_token),
            "http.viewer_denied",
        )

        registered = client.post(
            "/mcp/servers",
            headers=_headers(admin, "register", idempotency=True),
            json={
                "name": "PRD-082 Runtime MCP",
                "endpoint_url": endpoint,
                "auth": {"type": "bearer", "bearer_token": runtime.bearer_token},
            },
        )
        registered_body = _assert_http_contract(registered, 202, "http.register")
        _require_response_private(registered, (endpoint, runtime.bearer_token), "http.register")
        server = registered_body.get("server")
        operation = registered_body.get("operation")
        require(type(server) is dict and type(operation) is dict, "http.register", "shape")
        require(str(server.get("mcp_server_id")).startswith("mcps_"), "http.register", "server")
        operation_id = str(operation.get("mcp_server_operation_id"))
        operation_row = _run_until_operation_terminal(config, graph, operation_id)
        register_terminal = str(operation_row["error_code"] or operation_row["status"])
        require(
            operation_row["status"] == "succeeded" and operation_row["error_code"] is None,
            "scheduler.register",
            register_terminal
            if register_terminal
            in {
                "mcp.credential_invalid",
                "mcp.executor_disabled",
                "mcp.schema_invalid",
                "mcp.state_conflict",
                "mcp.sync_failed",
                "mcp.unavailable",
                "succeeded",
            }
            else "terminal",
        )

        tools_response = client.get("/mcp/tools", headers=_headers(developer, "tools"))
        tools_body = _assert_http_contract(tools_response, 200, "http.tools")
        tools = tools_body.get("tools")
        require(type(tools) is list and len(tools) == 1, "http.tools", "count")
        tool_id = str(tools[0].get("mcp_tool_id"))
        require(tools[0].get("status") == "review_required", "http.tools", "review")

        cross = client.get(f"/mcp/tools/{tool_id}", headers=_headers(other, "cross-tenant"))
        _assert_http_contract(cross, 404, "http.cross_tenant", code="mcp.tool_not_found")

        policy = client.patch(
            f"/mcp/tools/{tool_id}",
            headers=_headers(admin, "policy-standard"),
            json={
                "expected_policy_revision": 1,
                "status": "active",
                "risk_level": "standard",
                "approval_required": False,
                "required_permission_codes": [],
                "allowed_role_ids": [graph.developer_role_id],
                "allowed_user_ids": [],
                "result_redaction_pointers": [],
            },
        )
        policy_body = _assert_http_contract(policy, 200, "http.policy_standard")
        require(
            policy_body.get("tool", {}).get("policy_revision") == 2,
            "http.policy_standard",
            "revision",
        )

        terminal: dict[str, sqlite3.Row] = {}
        for label in ("success", "log_only", "known_error", "output_block"):
            _, created = _invoke(client, developer, tool_id, label, arguments[label])
            invocation_id = str(created.get("invocation", {}).get("mcp_invocation_id"))
            terminal[label] = _run_until_invocation_terminal(config, graph, invocation_id)

        require(
            terminal["success"]["status"] == "succeeded"
            and terminal["success"]["input_dlp_status"] == "clean",
            "governance.clean",
            "terminal",
        )
        require(
            terminal["log_only"]["status"] == "succeeded"
            and terminal["log_only"]["input_dlp_status"] == "log_only",
            "governance.log_only",
            "terminal",
        )
        require(
            terminal["known_error"]["status"] == "failed"
            and terminal["known_error"]["error_code"] == "mcp.tool_failed"
            and terminal["known_error"]["execution_status"] == "called",
            "governance.known_error",
            "terminal",
        )
        require(
            terminal["output_block"]["status"] == "blocked"
            and terminal["output_block"]["error_code"] == "mcp.result_blocked"
            and terminal["output_block"]["output_dlp_status"] == "blocked",
            "governance.output_block",
            "terminal",
        )

        success_id = str(terminal["success"]["mcp_invocation_id"])
        result_response = client.get(
            f"/mcp/invocations/{success_id}/result",
            headers=_headers(developer, "success-result"),
        )
        _assert_http_contract(result_response, 200, "http.success_result")
        require(runtime.result_canary in result_response.text, "http.success_result", "value")

        before_block = _call_counts(counter)
        blocked = client.post(
            f"/mcp/tools/{tool_id}/invoke",
            headers=_headers(developer, "blocked", idempotency=True),
            json={"arguments": {"mode": "blocked", "payload": arguments["blocked"]}},
        )
        _assert_http_contract(blocked, 403, "http.input_block", code="mcp.arguments_blocked")
        _require_response_private(blocked, (arguments["blocked"],), "http.input_block")
        after_block = _call_counts(counter)
        require(before_block == after_block, "governance.input_block", "network_call")

        policy_high = client.patch(
            f"/mcp/tools/{tool_id}",
            headers=_headers(admin, "policy-high"),
            json={
                "expected_policy_revision": 2,
                "status": "active",
                "risk_level": "high",
                "approval_required": True,
                "approval_expires_seconds": 600,
                "required_permission_codes": [],
                "allowed_role_ids": [graph.developer_role_id],
                "allowed_user_ids": [],
                "result_redaction_pointers": [
                    "/content/0/text",
                    "/structuredContent/value",
                ],
            },
        )
        high_body = _assert_http_contract(policy_high, 200, "http.policy_high")
        require(
            high_body.get("tool", {}).get("policy_revision") == 3, "http.policy_high", "revision"
        )

        _, approval_created = _invoke(client, developer, tool_id, "approved", arguments["approved"])
        approval_invocation = approval_created.get("invocation", {})
        require(
            approval_invocation.get("status") == "approval_pending"
            and approval_invocation.get("tool_call_count") == 0,
            "approval.pending",
            "contract",
        )
        approval_id = str(approval_invocation.get("approval_task_id"))
        approval_invocation_id = str(approval_invocation.get("mcp_invocation_id"))
        verified = client.post(
            f"/approvals/{approval_id}/step-up",
            headers=_headers(security, "approval-step-up"),
            json={"action": "approve", "password": runtime.role_password},
        )
        require(verified.status_code == 200, "approval.step_up", "status")
        step_up_token = str(_json_object(verified, "approval.step_up").get("step_up_token"))
        require(len(step_up_token) > 32, "approval.step_up", "token")
        private_runtime.append(step_up_token)
        approved = client.post(
            f"/approvals/{approval_id}/approve",
            headers=_headers(security, "approval-decide", idempotency=True),
            json={
                "expected_revision": 1,
                "reason_code": "approved_business_need",
                "step_up_token": step_up_token,
            },
        )
        require(approved.status_code == 200, "approval.decide", "status")
        approved_body = _json_object(approved, "approval.decide")
        require(
            approved_body.get("approval", {}).get("status") == "approved"
            and approved_body.get("decision", {}).get("step_up_used") is True,
            "approval.decide",
            "contract",
        )
        approved_terminal = _run_until_invocation_terminal(config, graph, approval_invocation_id)
        require(
            approved_terminal["status"] == "succeeded"
            and int(approved_terminal["result_redaction_count"]) == 2,
            "approval.execution",
            "terminal",
        )
        approved_result = client.get(
            f"/mcp/invocations/{approval_invocation_id}/result",
            headers=_headers(developer, "approved-result"),
        )
        _assert_http_contract(approved_result, 200, "http.approved_result")
        require(
            runtime.result_canary not in approved_result.text
            and "[REDACTED]" in approved_result.text,
            "http.approved_result",
            "redaction",
        )

        policy_recovery = client.patch(
            f"/mcp/tools/{tool_id}",
            headers=_headers(admin, "policy-recovery"),
            json={
                "expected_policy_revision": 3,
                "status": "active",
                "risk_level": "standard",
                "approval_required": False,
                "required_permission_codes": [],
                "allowed_role_ids": [graph.developer_role_id],
                "allowed_user_ids": [],
                "result_redaction_pointers": [],
            },
        )
        recovery_body = _assert_http_contract(policy_recovery, 200, "http.policy_recovery")
        require(
            recovery_body.get("tool", {}).get("policy_revision") == 4,
            "http.policy_recovery",
            "revision",
        )

        _, crash_created = _invoke(client, developer, tool_id, "crash", arguments["crash"])
        crash_invocation_id = str(crash_created.get("invocation", {}).get("mcp_invocation_id"))
        worker_environment = dict(environment)
        worker_environment["METAHUB_MCP_SCHEDULER_ENABLED"] = "true"
        worker = processes.add(
            PROCESS.start_process(
                "mcp-crash-worker",
                [str(PYTHON), str(SCRIPT), "--scheduler-once"],
                work=work,
                environment=worker_environment,
            )
        )

        def calling(connection: sqlite3.Connection) -> sqlite3.Row | None:
            return connection.execute(
                "SELECT * FROM mcp_invocations WHERE mcp_invocation_id=? AND status='calling'",
                (crash_invocation_id,),
            ).fetchone()

        calling_row = _wait_database(graph, calling, "recovery.calling", timeout_seconds=15)
        require(worker.process.poll() is None, "recovery.calling", "worker_exited")
        call_started_deadline = time.monotonic() + 5
        while _call_counts(counter)["crash"] == 0 and time.monotonic() < call_started_deadline:
            require(worker.process.poll() is None, "recovery.remote_call", "worker_exited")
            time.sleep(0.05)
        require(_call_counts(counter)["crash"] == 1, "recovery.remote_call", "not_started")
        processes.stop(worker)
        lease_expires = datetime.fromisoformat(
            str(calling_row["lease_expires_at"]).replace("Z", "+00:00")
        )
        while datetime.now(UTC) <= lease_expires:
            time.sleep(0.2)
        crash_terminal = _run_until_invocation_terminal(config, graph, crash_invocation_id)
        require(
            crash_terminal["status"] == "outcome_unknown"
            and crash_terminal["error_code"] == "mcp.outcome_unknown"
            and crash_terminal["execution_status"] == "unknown"
            and crash_terminal["tool_call_count"] is None,
            "recovery.outcome_unknown",
            "terminal",
        )

        _drain_mcp_outbox(config, graph)

        def approval_drained(connection: sqlite3.Connection) -> sqlite3.Row | None:
            pending = connection.execute(
                "SELECT COUNT(*) FROM approval_event_outbox WHERE status IN('pending','processing')"
            ).fetchone()[0]
            writebacks = connection.execute(
                "SELECT COUNT(*) FROM approval_writebacks WHERE status IN('pending','processing')"
            ).fetchone()[0]
            return connection.execute("SELECT 1").fetchone() if pending + writebacks == 0 else None

        _wait_database(graph, approval_drained, "approval.drain", timeout_seconds=20)

    counts = _call_counts(counter)
    require(
        counts["direct_success"] == 1
        and counts["direct_error"] == 1
        and counts["success"] == 1
        and counts["log_only"] == 1
        and counts["known_error"] == 1
        and counts["output_block"] == 1
        and counts["approved"] == 1
        and counts["crash"] == 1,
        "mcp.counter",
        "counts",
    )
    with closing(connect_database(graph.database_url)) as connection:
        approval_binding = connection.execute(
            """
            SELECT COUNT(*) FROM approval_tasks task
            JOIN approval_decisions decision
              ON decision.tenant_id=task.tenant_id
             AND decision.approval_task_id=task.approval_task_id
            JOIN approval_writebacks writeback
              ON writeback.tenant_id=task.tenant_id
             AND writeback.approval_task_id=task.approval_task_id
            WHERE task.target_type='mcp_tool_invocation'
              AND task.status='approved' AND task.risk_level='high'
              AND task.required_permission_code='mcp.approve'
              AND task.created_by_id!=decision.actor_id
              AND decision.step_up_used=1 AND writeback.status='completed'
            """
        ).fetchone()[0]
        require(int(approval_binding) == 1, "approval.binding", "facts")
        audit_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE tenant_id=? AND action LIKE 'mcp.%'",
                (graph.tenant_id,),
            ).fetchone()[0]
        )
        require(audit_count >= 4, "traceability.audit", "count")
    return (
        {
            "api": {
                "real_control_http": True,
                "backend_rbac_denied": True,
                "tenant_isolation_hidden": True,
                "uniform_request_id": True,
                "private_no_store": True,
            },
            "governance": {
                "clean_succeeded": True,
                "log_only_succeeded": True,
                "input_block_zero_call": True,
                "output_blocked": True,
                "high_risk_approval_completed": True,
                "non_self_approval": True,
                "step_up_consumed": True,
                "known_success": True,
                "known_error_code": "mcp.tool_failed",
                "unknown_error_code": "mcp.outcome_unknown",
                "expired_call_recovered": True,
                "redaction_applied": True,
            },
            "traceability": {
                "remote_call_count": sum(counts.values()),
                "audit_row_count": audit_count,
                "approval_binding_count": int(approval_binding),
            },
        },
        tuple(private_runtime),
    )


def _final_database_facts(graph: RuntimeGraph) -> tuple[dict[str, object], dict[str, object]]:
    with closing(connect_database(graph.database_url)) as connection:
        status_rows = connection.execute(
            "SELECT status,COUNT(*) AS count FROM mcp_invocations GROUP BY status"
        ).fetchall()
        statuses = {str(row["status"]): int(row["count"]) for row in status_rows}
        attempts = int(
            connection.execute("SELECT COUNT(*) FROM mcp_invocation_attempts").fetchone()[0]
        )
        events = int(connection.execute("SELECT COUNT(*) FROM mcp_invocation_events").fetchone()[0])
        outboxes = int(
            connection.execute("SELECT COUNT(*) FROM mcp_invocation_event_outbox").fetchone()[0]
        )
        terminal_outboxes = int(
            connection.execute(
                "SELECT COUNT(*) FROM mcp_invocation_event_outbox "
                "WHERE status IN('succeeded','failed')"
            ).fetchone()[0]
        )
        nonterminal = (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM mcp_server_operations "
                    "WHERE status IN('queued','processing')"
                ).fetchone()[0]
            )
            + int(
                connection.execute(
                    "SELECT COUNT(*) FROM mcp_server_sync_jobs "
                    "WHERE status IN('pending','processing')"
                ).fetchone()[0]
            )
            + int(
                connection.execute(
                    "SELECT COUNT(*) FROM mcp_invocations "
                    "WHERE status IN('approval_pending','queued','probing','calling')"
                ).fetchone()[0]
            )
            + int(
                connection.execute(
                    "SELECT COUNT(*) FROM mcp_invocation_event_outbox "
                    "WHERE status IN('pending','processing')"
                ).fetchone()[0]
            )
            + int(
                connection.execute(
                    "SELECT COUNT(*) FROM mcp_invocation_attempts WHERE status='processing'"
                ).fetchone()[0]
            )
        )
        active_leases = (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM mcp_scheduler_tenants WHERE status='leased' "
                    "OR lease_owner_id IS NOT NULL OR lease_token IS NOT NULL "
                    "OR lease_expires_at IS NOT NULL"
                ).fetchone()[0]
            )
            + int(
                connection.execute(
                    "SELECT COUNT(*) FROM mcp_server_sync_jobs WHERE lease_owner_id IS NOT NULL "
                    "OR lease_token IS NOT NULL OR lease_expires_at IS NOT NULL"
                ).fetchone()[0]
            )
            + int(
                connection.execute(
                    "SELECT COUNT(*) FROM mcp_invocations WHERE lease_owner_id IS NOT NULL "
                    "OR lease_token IS NOT NULL OR lease_expires_at IS NOT NULL"
                ).fetchone()[0]
            )
            + int(
                connection.execute(
                    "SELECT COUNT(*) FROM mcp_invocation_event_outbox "
                    "WHERE lease_owner_id IS NOT NULL "
                    "OR lease_token IS NOT NULL OR lease_expires_at IS NOT NULL"
                ).fetchone()[0]
            )
        )
        failed_tracking = int(
            connection.execute(
                "SELECT COUNT(*) FROM mcp_invocations "
                "WHERE status IN('failed','blocked','outcome_unknown')"
            ).fetchone()[0]
        )
        cost_tracking = int(
            connection.execute(
                "SELECT COUNT(*) FROM mcp_invocations "
                "WHERE cost_status IN('not_applicable','not_reported','unknown')"
            ).fetchone()[0]
        )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    require(nonterminal == 0, "database.final", "nonterminal")
    require(active_leases == 0, "database.final", "leases")
    require(integrity == "ok" and foreign_keys == 0, "database.final", "health")
    require(events == outboxes == terminal_outboxes and events >= 6, "database.final", "events")
    return (
        {
            "invocation_count": sum(statuses.values()),
            "succeeded_count": statuses.get("succeeded", 0),
            "failed_count": statuses.get("failed", 0),
            "blocked_count": statuses.get("blocked", 0),
            "outcome_unknown_count": statuses.get("outcome_unknown", 0),
            "attempt_count": attempts,
            "event_count": events,
            "outbox_terminal_count": terminal_outboxes,
            "failed_request_tracking_count": failed_tracking,
            "cost_status_tracking_count": cost_tracking,
        },
        {
            "integrity": integrity,
            "foreign_key_violations": foreign_keys,
            "nonterminal_rows": nonterminal,
            "active_leases": active_leases,
        },
    )


def _quiesce_database(graph: RuntimeGraph) -> None:
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


def source_provenance() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in SOURCE_FILES.items():
        require(path.is_file() and not path.is_symlink(), "evidence.source", "missing")
        path.resolve().relative_to(ROOT.resolve())
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def assert_safe_evidence(value: object, private_values: tuple[str, ...]) -> None:
    def visit(item: object) -> None:
        if type(item) is dict:
            for key, child in item.items():
                if str(key).casefold() in FORBIDDEN_EVIDENCE_KEYS:
                    raise AcceptanceFailure("evidence.shape", "forbidden_key")
                visit(child)
        elif type(item) in {list, tuple}:
            for child in item:
                visit(child)

    visit(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    require(
        not any(private and private in encoded for private in private_values),
        "evidence.privacy",
        "private",
    )


def empty_evidence_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "prd_id": "PRD-082",
        "status": "incomplete",
        "generated_at": utc_now(),
        "all_108_prds_complete": False,
        "migration": {},
        "services": {},
        "transport": {},
        "api": {},
        "governance": {},
        "traceability": {},
        "database": {},
        "privacy": {},
        "cleanup": {},
        "source_provenance": {},
    }


def _exact_scalar_mapping(value: object, expected: dict[str, object]) -> bool:
    return (
        type(value) is dict
        and set(value) == set(expected)
        and all(
            type(value[name]) is type(expected_value) and value[name] == expected_value
            for name, expected_value in expected.items()
        )
    )


def _validate_evidence(value: dict[str, object]) -> None:
    require(set(value) == EVIDENCE_TOP_LEVEL_KEYS, "evidence.shape", "top_level")
    require(
        type(value.get("schema_version")) is int and value["schema_version"] == 1,
        "evidence.shape",
        "schema",
    )
    require(
        type(value.get("prd_id")) is str and value["prd_id"] == "PRD-082", "evidence.shape", "prd"
    )
    require(
        type(value.get("status")) is str and value["status"] == "passed", "evidence.shape", "status"
    )
    require(value.get("all_108_prds_complete") is False, "evidence.shape", "global")
    require(
        type(value.get("generated_at")) is str
        and _UTC_TIMESTAMP.fullmatch(str(value["generated_at"])) is not None,
        "evidence.shape",
        "generated_at",
    )
    migration = value.get("migration")
    require(
        type(migration) is dict and set(migration) == {"fresh", "repeat", "down", "reup"},
        "evidence.shape",
        "migration",
    )
    for name, count, version in (
        ("fresh", 75, "0075"),
        ("repeat", 75, "0075"),
        ("down", 74, "0074"),
        ("reup", 75, "0075"),
    ):
        item = migration[name]
        require(
            _exact_scalar_mapping(
                item,
                {
                    "count": count,
                    "version": version,
                    "integrity": "ok",
                    "foreign_key_violations": 0,
                },
            ),
            "evidence.shape",
            f"migration_{name}",
        )
    require(
        _exact_scalar_mapping(value.get("services"), {name: True for name in SERVICE_FACTS}),
        "evidence.shape",
        "services",
    )
    require(
        _exact_scalar_mapping(value.get("transport"), {name: True for name in TRANSPORT_FACTS}),
        "evidence.shape",
        "transport",
    )
    require(
        _exact_scalar_mapping(value.get("api"), {name: True for name in API_FACTS}),
        "evidence.shape",
        "api",
    )
    require(
        _exact_scalar_mapping(value.get("governance"), GOVERNANCE_FACTS),
        "evidence.shape",
        "governance",
    )
    require(
        _exact_scalar_mapping(value.get("traceability"), TRACEABILITY_FACTS),
        "evidence.shape",
        "traceability",
    )
    require(
        _exact_scalar_mapping(value.get("database"), DATABASE_FACTS),
        "evidence.shape",
        "database",
    )
    require(
        _exact_scalar_mapping(value.get("privacy"), PRIVACY_FACTS),
        "evidence.shape",
        "privacy",
    )
    require(
        _exact_scalar_mapping(value.get("cleanup"), CLEANUP_FACTS),
        "evidence.shape",
        "cleanup",
    )
    provenance = value.get("source_provenance")
    require(
        type(provenance) is dict
        and set(provenance) == set(SOURCE_FILES)
        and all(
            type(digest) is str
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            for digest in provenance.values()
        ),
        "evidence.shape",
        "source_provenance",
    )
    require(provenance == source_provenance(), "evidence.source", "changed")


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
    claim = staging / OWNER_FILENAME
    require(
        staging.is_dir()
        and not staging.is_symlink()
        and claim.is_file()
        and not claim.is_symlink()
        and claim.read_text(encoding="ascii").strip() == owner
        and payload["source_provenance"] == expected_provenance
        and not final.exists()
        and {item.name for item in staging.iterdir()} == {OWNER_FILENAME},
        "evidence.path",
        "owner",
    )
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    evidence = staging / "runtime-acceptance.json"
    manifest = staging / "SHA256SUMS.txt"
    with evidence.open("xb") as stream:
        stream.write(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    with manifest.open("x", encoding="ascii", newline="\n") as stream:
        stream.write(f"{digest}  runtime-acceptance.json\n")
    require(
        hashlib.sha256(evidence.read_bytes()).hexdigest() == digest, "evidence.hash", "mismatch"
    )
    require(
        {item.name for item in staging.iterdir()} == {OWNER_FILENAME, evidence.name, manifest.name},
        "evidence.path",
        "entries",
    )
    BASE.scan_private_storage(staging, private_values)
    require(source_provenance() == expected_provenance, "evidence.source", "changed")
    staging.replace(final)
    try:
        BASE.scan_private_storage(final, private_values)
        require(source_provenance() == expected_provenance, "evidence.source", "changed")
        require(
            {item.name for item in final.iterdir()}
            == {OWNER_FILENAME, evidence.name, manifest.name},
            "evidence.final",
            "entries",
        )
        (final / OWNER_FILENAME).unlink()
        return final / evidence.name
    except BaseException:
        if final.exists():
            _remove_owned_tree(final, ARTIFACT_ROOT, owner, stage="cleanup.failed_final")
        raise


def _start_mcp_server(work: Path, environment: dict[str, str], processes: OwnedProcesses) -> str:
    require(PYTHON.is_file(), "process.mcp", "python_missing")
    process = processes.add(
        PROCESS.start_process(
            "mcp-server",
            [str(PYTHON), str(SCRIPT), "--serve-mcp", "--mcp-port", str(MCP_PORT)],
            work=work,
            environment=environment,
        )
    )
    _wait_port(process, MCP_PORT, "health.mcp")
    return f"http://{LOOPBACK}:{MCP_PORT}/mcp"


def run(run_id: str | None = None) -> Path:
    actual_run_id = _safe_run_id(run_id)
    owner = hashlib.sha256(secrets.token_bytes(48)).hexdigest()
    runtime = RuntimeSecrets.create()
    before = source_provenance()
    _assert_ports_available()
    work, staging, final = _create_directories(actual_run_id, owner)
    graph: RuntimeGraph | None = None
    processes = OwnedProcesses()
    try:
        graph = _prepare_database(work, runtime)
        control_environment = _service_environment(work, graph, runtime, scheduler_enabled=False)
        config = _load_scheduler_config(control_environment)
        require(_production_executor_zero_network(config), "executor.production", "network")
        endpoint = _start_mcp_server(work, control_environment, processes)
        transport = _exercise_official_sdk(config, endpoint, runtime)
        http_facts, runtime_private = _exercise_http_runtime(
            work,
            graph,
            runtime,
            control_environment,
            config,
            processes,
            endpoint,
        )
        processes.close()
        _wait_ports_released()
        _quiesce_database(graph)
        traceability, database = _final_database_facts(graph)
        private_values = runtime.values(
            (endpoint, f"http://{LOOPBACK}:{MCP_PORT}", *runtime_private)
        )
        privacy = BASE.scan_private_storage(work, private_values)
        require(source_provenance() == before, "evidence.source", "changed")
        _remove_owned_tree(work, WORK_ROOT, owner, stage="cleanup.work")
        payload = empty_evidence_payload()
        payload["status"] = "passed"
        payload["generated_at"] = utc_now()
        payload["migration"] = graph.migration
        payload["services"] = {
            "control_http": True,
            "streamable_http_mcp": True,
            "official_sdk_loopback": True,
            "production_executor_zero_network": True,
        }
        payload["transport"] = transport
        payload["api"] = http_facts["api"]
        payload["governance"] = http_facts["governance"]
        payload["traceability"] = {
            **http_facts["traceability"],
            **traceability,
        }
        payload["database"] = database
        payload["privacy"] = privacy
        payload["cleanup"] = {
            "owned_processes_remaining": len(processes.items),
            "fixed_ports_released": True,
            "sqlite_sidecars_remaining": int(privacy["sqlite_sidecars_remaining"]),
            "work_removed": True,
        }
        payload["source_provenance"] = before
        evidence = _write_evidence(
            staging,
            final,
            owner,
            payload,
            private_values,
            before,
        )
        return evidence
    except BaseException:
        try:
            processes.close()
        except Exception:
            pass
        if graph is not None:
            try:
                _quiesce_database(graph)
            except Exception:
                pass
        try:
            _wait_ports_released()
        except Exception:
            pass
        for path, root in (
            (final, ARTIFACT_ROOT),
            (staging, ARTIFACT_ROOT),
            (work, WORK_ROOT),
        ):
            try:
                if path.exists():
                    _remove_owned_tree(path, root, owner, stage="cleanup.failed")
            except Exception:
                pass
        raise


def _serve_mcp(port: int) -> int:
    if port != MCP_PORT:
        return 1
    counter_raw = os.environ.get("PRD082_MCP_COUNTER_FILE")
    result_value = os.environ.get("PRD082_MCP_RESULT_VALUE")
    output_block_value = os.environ.get("PRD082_MCP_OUTPUT_BLOCK_VALUE")
    if not counter_raw or not result_value or not output_block_value:
        return 1
    counter = Path(counter_raw).resolve()
    try:
        counter.relative_to(WORK_ROOT.resolve())
    except ValueError:
        return 1
    if counter.exists() or counter.parent.name == "":
        return 1

    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.types import ToolAnnotations

    server = FastMCP(
        "MetaHub PRD-082 Runtime",
        host=LOOPBACK,
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        log_level="ERROR",
    )

    @server.tool(
        name="governed_tool",
        description="Bounded PRD-082 runtime contract tool",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
        structured_output=True,
    )
    async def governed_tool(mode: str, payload: str) -> RuntimeMcpResult:
        """Return a bounded structured result for the acceptance workflow."""

        del payload
        safe_mode = mode if mode in _CALL_MODES else ""
        if not safe_mode:
            raise ToolError("unsupported runtime mode")
        with counter.open("a", encoding="ascii", newline="\n") as stream:
            stream.write(safe_mode + "\n")
        if safe_mode in {"direct_error", "known_error"}:
            raise ToolError("fixed runtime tool failure")
        if safe_mode == "crash":
            import asyncio

            await asyncio.sleep(60)
        value = output_block_value if safe_mode == "output_block" else result_value
        return RuntimeMcpResult(mode=safe_mode, value=value)

    server.run(transport="streamable-http")
    return 0


def _scheduler_once() -> int:
    try:
        config = load_app_config("control-plane-api")
        run_mcp_scheduler_once(config, worker_id="prd082-runtime-crash-worker")
    except Exception:
        return 1
    return 0


def parse_arguments(argv: list[str] | None = None) -> Any:
    parser = SafeArgumentParser(description="Run PRD-082 runtime acceptance")
    parser.add_argument("--run-id")
    parser.add_argument("--serve-mcp", action="store_true", help=__import__("argparse").SUPPRESS)
    parser.add_argument(
        "--scheduler-once", action="store_true", help=__import__("argparse").SUPPRESS
    )
    parser.add_argument(
        "--mcp-port", type=int, default=MCP_PORT, help=__import__("argparse").SUPPRESS
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parse_arguments(argv)
        if arguments.serve_mcp:
            return _serve_mcp(arguments.mcp_port)
        if arguments.scheduler_once:
            return _scheduler_once()
        evidence = run(arguments.run_id)
    except AcceptanceFailure as exc:
        print(f"PRD-082 runtime acceptance failed: {exc.stage}:{exc.code}", file=sys.stderr)
        return 1
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, RuntimeError, TypeError):
        print("PRD-082 runtime acceptance failed: runtime:dependency_failure", file=sys.stderr)
        return 1
    except Exception:
        print("PRD-082 runtime acceptance failed: runtime:unexpected_failure", file=sys.stderr)
        return 1
    print(evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
