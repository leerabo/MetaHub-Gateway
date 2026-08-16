"""Owner-safe PAGE-114 browser harness for PRD-082.

The harness owns one fresh SQLite database plus the exact Control and Console
processes used by the Chromium driver. Endpoint URLs, credentials, arguments,
tokens, prompts, responses, and result bodies are never published.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import socket
import sys
import threading
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[2]
WORK_ROOT = ROOT / ".tmp" / "prd082-browser-harness"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd082-browser"
LOOPBACK = "127.0.0.1"
HARNESS_PORT = 18885
OWNER_FILENAME = ".prd082-browser-owner"
ADMIN_PASSWORD_ENV = "PRD082_BROWSER_ADMIN_PASSWORD"
ROLE_PASSWORD_ENV = "PRD082_BROWSER_ROLE_PASSWORD"
STOP_TOKEN_ENV = "PRD082_BROWSER_STOP_TOKEN"
MAX_COMPLETE_BYTES = 24 * 1024
MAX_PRIVATE_BYTES = 8 * 1024
EXPECTED_MIGRATION_COUNT = 75
EXPECTED_MIGRATION_VERSION = "0075"
PREVIOUS_MIGRATION_VERSION = "0074"
ADMIN_EMAIL = "admin@metahub.local"
VIEWER_EMAIL = "viewer-prd082@local.invalid"
VIEWER_USER_ID = "usr_prd082_browser_viewer"
OPERATOR_EMAIL = "operator-prd082@local.invalid"
OPERATOR_USER_ID = "usr_prd082_browser_operator"
POLICY_OPERATOR_EMAIL = "policy-operator-prd082@local.invalid"
POLICY_OPERATOR_USER_ID = "usr_prd082_browser_policy_operator"
SCREENSHOTS = (
    "page-114-servers-desktop-1280x720.png",
    "page-114-tools-desktop-1280x720.png",
    "page-114-invocations-mobile-390x844.png",
    "page-114-register-mobile-390x844.png",
)
ALLOWED_ARTIFACTS = frozenset((*SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json"))
SOURCE_FILES = {
    "browser_harness": Path(__file__).resolve(),
    "browser_driver": ROOT / "scripts" / "acceptance" / "prd082-browser-acceptance.mjs",
    "browser_helper_prd080": ROOT / "scripts" / "acceptance" / "prd080-browser-acceptance.mjs",
    "browser_helper_prd078": ROOT / "scripts" / "acceptance" / "prd078-browser-acceptance.mjs",
    "browser_helper_prd066": ROOT / "scripts" / "acceptance" / "prd066-browser-acceptance.mjs",
    "browser_helper_prd056": ROOT / "scripts" / "acceptance" / "prd056-browser-acceptance.mjs",
    "runtime_helper_prd078": ROOT / "scripts" / "acceptance" / "prd078-runtime-acceptance.py",
    "runtime_helper_prd075": ROOT / "scripts" / "acceptance" / "prd075-runtime-acceptance.py",
    "harness_contract": ROOT
    / "scripts"
    / "acceptance"
    / "tests"
    / "test_prd082_browser_harness.py",
    "driver_contract": ROOT / "console-web" / "tests" / "unit" / "prd082BrowserAcceptance.test.mjs",
    "migration_up": ROOT / "infra" / "migrations" / "0075_mcp_tool_registration_permissions.sql",
    "migration_down": ROOT
    / "infra"
    / "migrations"
    / "0075_mcp_tool_registration_permissions.down.sql",
    "console_main": ROOT / "console-web" / "src" / "app" / "main.js",
    "mcp_client": ROOT / "console-web" / "src" / "lib" / "mcpTools.js",
    "mcp_state": ROOT / "console-web" / "src" / "features" / "mcp-tools" / "mcpToolPageState.js",
    "mcp_binding": ROOT / "console-web" / "src" / "features" / "mcp-tools" / "bindMcpTools.js",
    "mcp_renderer": ROOT / "console-web" / "src" / "features" / "mcp-tools" / "renderMcpTools.js",
    "mcp_routes": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "mcp_governance"
    / "routes.py",
}


def _load_runtime() -> Any:
    name = "_metahub_prd078_runtime_for_prd082_browser"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd078-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("browser runtime unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


RUNTIME = _load_runtime()
AcceptanceFailure = RUNTIME.AcceptanceFailure
SafeArgumentParser = RUNTIME.SafeArgumentParser
require = RUNTIME.require

from metahub_shared.db import (  # noqa: E402
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.security import hash_password  # noqa: E402
from metahub_shared.rbac import permission_matrix_sha256  # noqa: E402


@dataclass(frozen=True, slots=True)
class BrowserSecrets:
    runtime: RUNTIME.RuntimeSecrets = field(repr=False)
    stop_token: str = field(repr=False)
    mcp_encryption_secret: str = field(repr=False)
    mcp_hmac_secret: str = field(repr=False)
    invocation_argument: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> BrowserSecrets:
        admin_password = os.environ.get(ADMIN_PASSWORD_ENV, "")
        role_password = os.environ.get(ROLE_PASSWORD_ENV, "")
        stop_token = os.environ.get(STOP_TOKEN_ENV, "")
        require(len(admin_password) >= 32, "config.admin_password", "invalid")
        require(len(role_password) >= 32, "config.role_password", "invalid")
        require(len(stop_token) >= 43, "config.stop_token", "invalid")
        generated = RUNTIME.RuntimeSecrets.create()

        def private(label: str) -> str:
            return f"Prd082-{label}-{secrets.token_urlsafe(48)}"

        return cls(
            runtime=replace(
                generated,
                admin_password=admin_password,
                role_password=role_password,
            ),
            stop_token=stop_token,
            mcp_encryption_secret=private("McpEncryption"),
            mcp_hmac_secret=private("McpHmac"),
            invocation_argument=private("InvocationArgument"),
        )

    def values(self, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        return self.runtime.values(
            (
                self.stop_token,
                self.mcp_encryption_secret,
                self.mcp_hmac_secret,
                self.invocation_argument,
                *extra,
            )
        )


@dataclass(frozen=True, slots=True)
class BrowserGraph:
    database: Path = field(repr=False)
    database_url: str = field(repr=False)
    tenant_id: str
    admin_user_id: str
    admin_role_id: str
    revocable_role_id: str
    server_id: str
    policy_tool_id: str
    high_tool_id: str
    migration: dict[str, object]


def _digest(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def _id(prefix: str, marker: str) -> str:
    return prefix + _digest(marker)[:32]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _direct_child(root: Path, name: str, *, stage: str) -> Path:
    require(root.resolve().is_relative_to(ROOT.resolve()), stage, "root")
    require(Path(name).name == name and name not in {".", ".."}, stage, "name")
    target = (root / name).resolve()
    require(target.parent == root.resolve(), stage, "boundary")
    return target


def _create_owned_directory(path: Path, root: Path, owner: str, *, stage: str) -> None:
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


def create_run_directories(run_id: str, owner: str) -> tuple[Path, Path, Path]:
    require(
        len(owner) == 64 and all(item in "0123456789abcdef" for item in owner),
        "config.owner",
        "invalid",
    )
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    require(not WORK_ROOT.is_symlink() and not ARTIFACT_ROOT.is_symlink(), "config.root", "symlink")
    work = _direct_child(WORK_ROOT, run_id, stage="config.work")
    staging = _direct_child(ARTIFACT_ROOT, f".{run_id}.staging", stage="config.staging")
    final = _direct_child(ARTIFACT_ROOT, run_id, stage="config.final")
    require(
        not work.exists() and not staging.exists() and not final.exists(), "config.run", "exists"
    )
    claimed: list[tuple[Path, Path, str]] = []
    try:
        _create_owned_directory(work, WORK_ROOT, owner, stage="config.work_owner")
        claimed.append((work, WORK_ROOT, "cleanup.partial_work"))
        for name in ("files", "logs", "spool", "temp"):
            (work / name).mkdir()
        _create_owned_directory(staging, ARTIFACT_ROOT, owner, stage="config.staging_owner")
        claimed.append((staging, ARTIFACT_ROOT, "cleanup.partial_staging"))
    except Exception:
        for directory, root, stage in reversed(claimed):
            _remove_owned_directory(directory, root, owner, stage=stage)
        raise
    return work, staging, final


def _remove_owned_directory(path: Path, root: Path, owner: str, *, stage: str) -> None:
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


def _migration_state(database_url: str) -> dict[str, object]:
    with closing(connect_database(database_url)) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS item_count,MAX(version) AS version "
            "FROM schema_migrations WHERE status='applied'"
        ).fetchone()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    return {
        "count": int(row["item_count"]),
        "version": str(row["version"]),
        "integrity": integrity,
        "foreign_key_violations": foreign_keys,
    }


def _schema() -> dict[str, object]:
    return {
        "additionalProperties": False,
        "properties": {"payload": {"maxLength": 128, "type": "string"}},
        "required": ["payload"],
        "type": "object",
    }


def _insert_tool(
    connection: Any,
    *,
    tenant_id: str,
    server_id: str,
    actor_id: str,
    admin_role_id: str,
    marker: str,
    risk_level: str,
    timestamp: str,
) -> str:
    tool_id = _id("mcpt_", f"browser-tool-{marker}")
    schema_json = _canonical(_schema())
    annotations_json = _canonical({})
    created = timestamp
    activated = timestamp
    connection.execute(
        """
        INSERT INTO mcp_tools(
          mcp_tool_id,tenant_id,mcp_server_id,remote_name,title,description,
          input_schema_original_json,input_schema_original_sha256,
          input_schema_effective_json,input_schema_effective_sha256,
          output_schema_original_json,output_schema_original_sha256,
          output_schema_effective_json,output_schema_effective_sha256,
          annotations_json,annotations_sha256,schema_revision,policy_revision,
          risk_level,approval_required,approval_expires_seconds,
          required_permission_codes_json,result_redaction_pointers_json,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?, ?,?,?,?, NULL,NULL,NULL,NULL, ?,?,1,1,?,?,900,
                 '[]','[]','review_required',?,?,?,?)
        """,
        (
            tool_id,
            tenant_id,
            server_id,
            f"controlled_{marker}",
            f"Controlled {marker}",
            "Bounded schema used for PAGE-114 acceptance",
            schema_json,
            _digest(schema_json),
            schema_json,
            _digest(schema_json),
            annotations_json,
            _digest(annotations_json),
            risk_level,
            int(risk_level == "high"),
            actor_id,
            actor_id,
            created,
            created,
        ),
    )
    connection.execute(
        """
        INSERT INTO mcp_tool_role_grants(
          mcp_tool_role_grant_id,tenant_id,mcp_tool_id,role_id,created_by,created_at
        ) VALUES(?,?,?,?,?,?)
        """,
        (
            _id("mcprg_", f"browser-grant-{marker}"),
            tenant_id,
            tool_id,
            admin_role_id,
            actor_id,
            created,
        ),
    )
    connection.execute(
        """
        UPDATE mcp_tools SET status='active',policy_revision=2,updated_at=?
        WHERE tenant_id=? AND mcp_tool_id=?
        """,
        (activated, tenant_id, tool_id),
    )
    return tool_id


def prepare_database(work: Path, runtime: RUNTIME.RuntimeSecrets) -> BrowserGraph:
    database = _direct_child(work, "prd082-browser.sqlite3", stage="database.path")
    database_url = _database_url(database)
    fresh = apply_migrations(database_url)
    require(
        len(fresh.applied) == EXPECTED_MIGRATION_COUNT
        and fresh.applied[-1:] == [EXPECTED_MIGRATION_VERSION],
        "migration.fresh",
        "result",
    )
    fresh_state = _migration_state(database_url)
    require(
        fresh_state
        == {
            "count": 75,
            "version": "0075",
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
        "migration.fresh",
        "state",
    )
    repeat = apply_migrations(database_url)
    require(not repeat.applied and len(repeat.skipped) == 75, "migration.repeat", "result")
    repeat_state = _migration_state(database_url)
    down = rollback_last_migration(database_url, expected_version=EXPECTED_MIGRATION_VERSION)
    require(down.rolled_back == EXPECTED_MIGRATION_VERSION, "migration.down", "result")
    down_state = _migration_state(database_url)
    require(
        down_state["count"] == 74
        and down_state["version"] == PREVIOUS_MIGRATION_VERSION
        and down_state["integrity"] == "ok"
        and down_state["foreign_key_violations"] == 0,
        "migration.down",
        "state",
    )
    reup = apply_migrations(database_url)
    require(reup.applied == [EXPECTED_MIGRATION_VERSION], "migration.reup", "result")
    reup_state = _migration_state(database_url)
    require(reup_state == fresh_state, "migration.reup", "state")

    with RUNTIME.temporary_environment(
        {
            "METAHUB_SEED_ADMIN_PASSWORD": runtime.admin_password,
            "METAHUB_SEED_PROVIDER_API_KEY": runtime.provider_key,
            "METAHUB_SEED_VIRTUAL_KEY": runtime.virtual_key,
        }
    ):
        seed = apply_seed_data(database_url)

    connection = connect_database(database_url)
    try:
        actor = connection.execute(
            """
            SELECT user.user_id,role.role_id
            FROM users user
            JOIN roles role ON role.tenant_id=user.tenant_id AND role.code='admin'
            WHERE user.tenant_id=? AND user.email=?
            """,
            (seed.tenant_id, ADMIN_EMAIL),
        ).fetchone()
        require(actor is not None, "database.seed", "admin")
        operator_role = connection.execute(
            "SELECT role_id FROM roles WHERE tenant_id=? AND code='ops_admin'",
            (seed.tenant_id,),
        ).fetchone()
        require(operator_role is not None, "database.seed", "operator_role")
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for user_id, email, display_name, role in (
            (VIEWER_USER_ID, VIEWER_EMAIL, "PRD-082 Browser Viewer", "viewer"),
            (
                OPERATOR_USER_ID,
                OPERATOR_EMAIL,
                "PRD-082 Browser Operator",
                "ops_admin",
            ),
            (
                POLICY_OPERATOR_USER_ID,
                POLICY_OPERATOR_EMAIL,
                "PRD-082 Browser Policy Operator",
                "security_admin",
            ),
        ):
            connection.execute(
                """
                INSERT INTO users(
                  user_id,tenant_id,email,display_name,password_hash,role,status,
                  created_at,updated_at
                ) VALUES(?,?,?,?,?,?,'active',?,?)
                """,
                (
                    user_id,
                    seed.tenant_id,
                    email,
                    display_name,
                    hash_password(runtime.role_password),
                    role,
                    now,
                    now,
                ),
            )
        server_id = _id("mcps_", "browser-seeded-server")
        credential_id = _id("mcpc_", "browser-seeded-credential")
        actor_id = str(actor["user_id"])
        admin_role_id = str(actor["role_id"])
        connection.execute(
            """
            INSERT INTO mcp_servers(
              mcp_server_id,tenant_id,name,transport,endpoint_ciphertext,
              endpoint_encryption_key_version,endpoint_hmac,endpoint_hmac_key_version,
              endpoint_scheme,endpoint_host_hint,endpoint_has_path,
              active_credential_revision,protocol_version,capabilities_sha256,status,
              state_revision,created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,'Controlled Server','streamable_http','opaque-endpoint',
                     1,?,1,'https','m***.invalid',1,NULL,NULL,NULL,'pending_sync',1,
                     ?,?,?,?)
            """,
            (server_id, seed.tenant_id, _digest("browser-endpoint"), actor_id, actor_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO mcp_server_credentials(
              mcp_server_credential_id,tenant_id,mcp_server_id,credential_revision,
              auth_type,credential_ciphertext,credential_encryption_key_version,
              credential_hmac,credential_hmac_key_version,status,probe_operation_id,
              created_by,created_at,activated_at,rejected_at
            ) VALUES(?,?,?,1,'none',NULL,NULL,NULL,NULL,'candidate',NULL,?,?,NULL,NULL)
            """,
            (credential_id, seed.tenant_id, server_id, actor_id, now),
        )
        connection.execute(
            "UPDATE mcp_server_credentials SET status='active',activated_at=? "
            "WHERE tenant_id=? AND mcp_server_credential_id=?",
            (now, seed.tenant_id, credential_id),
        )
        connection.execute(
            """
            UPDATE mcp_servers
            SET active_credential_revision=1,protocol_version='2025-06-18',
                capabilities_sha256=?,status='active',state_revision=2,updated_at=?
            WHERE tenant_id=? AND mcp_server_id=?
            """,
            (_digest("browser-capabilities"), now, seed.tenant_id, server_id),
        )
        policy_tool_id = _insert_tool(
            connection,
            tenant_id=seed.tenant_id,
            server_id=server_id,
            actor_id=actor_id,
            admin_role_id=admin_role_id,
            marker="policy",
            risk_level="standard",
            timestamp=now,
        )
        high_tool_id = _insert_tool(
            connection,
            tenant_id=seed.tenant_id,
            server_id=server_id,
            actor_id=actor_id,
            admin_role_id=admin_role_id,
            marker="high",
            risk_level="high",
            timestamp=now,
        )
        connection.commit()
        require(
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            "database.seed",
            "integrity",
        )
        require(
            not connection.execute("PRAGMA foreign_key_check").fetchall(),
            "database.seed",
            "foreign_keys",
        )
    finally:
        connection.close()
    return BrowserGraph(
        database=database,
        database_url=database_url,
        tenant_id=seed.tenant_id,
        admin_user_id=actor_id,
        admin_role_id=admin_role_id,
        revocable_role_id=str(operator_role["role_id"]),
        server_id=server_id,
        policy_tool_id=policy_tool_id,
        high_tool_id=high_tool_id,
        migration={
            "fresh": fresh_state,
            "repeat": repeat_state,
            "down": down_state,
            "reup": reup_state,
        },
    )


DESKTOP_FACT_KEYS = {
    "width",
    "height",
    "nonblank",
    "page_114",
    "servers_visible",
    "tools_visible",
    "registration_submitted",
    "refresh_submitted",
    "server_disabled",
    "server_enabled",
    "schema_detail_verified",
    "policy_high_approval",
    "horizontal_overflow_free",
    "text_overflow_free",
    "overlap_count",
    "no_sensitive_dom",
    "next_content_hint",
}
MOBILE_FACT_KEYS = {
    "width",
    "height",
    "nonblank",
    "page_114",
    "invocation_summary_visible",
    "approval_summary_visible",
    "registration_dialog_native",
    "horizontal_overflow_free",
    "text_overflow_free",
    "overlap_count",
    "no_sensitive_dom",
    "next_content_hint",
}
LIFECYCLE_FACT_KEYS = {
    "backend_viewer_denied",
    "viewer_navigation_hidden",
    "viewer_page_hidden",
    "logout_cleared",
    "abort_observed",
    "logout_late_discarded",
    "revocation_root_cleared",
    "revocation_navigation_cleared",
    "revocation_late_discarded",
    "registered_private_values",
}


def _all_true(value: object, expected: set[str], *, stage: str) -> dict[str, object]:
    require(type(value) is dict and set(value) == expected, stage, "shape")
    for name, item in value.items():
        if name in {"width", "height", "overlap_count"}:
            continue
        require(item is True, stage, name)
    return value


def validate_browser_facts(value: object) -> dict[str, object]:
    require(type(value) is dict, "browser.complete", "shape")
    require(
        set(value)
        == {
            "status",
            "desktop",
            "mobile",
            "lifecycle",
            "network",
            "screenshots",
            "all_108_prds_complete",
        },
        "browser.complete",
        "shape",
    )
    require(
        value["status"] == "passed" and value["all_108_prds_complete"] is False,
        "browser.complete",
        "status",
    )
    desktop = _all_true(value["desktop"], DESKTOP_FACT_KEYS, stage="browser.desktop")
    mobile = _all_true(value["mobile"], MOBILE_FACT_KEYS, stage="browser.mobile")
    require(
        desktop["width"] == 1280 and desktop["height"] == 720 and desktop["overlap_count"] == 0,
        "browser.desktop",
        "layout",
    )
    require(
        mobile["width"] == 390 and mobile["height"] == 844 and mobile["overlap_count"] == 0,
        "browser.mobile",
        "layout",
    )
    _all_true(value["lifecycle"], LIFECYCLE_FACT_KEYS, stage="browser.lifecycle")
    network = value["network"]
    expected_network = {
        "external_requests",
        "forbidden_queries",
        "console_errors",
        "page_errors",
        "login_statuses",
        "logout_statuses",
        "registration_statuses",
        "refresh_statuses",
        "server_statuses",
        "policy_statuses",
        "list_successes",
        "detail_successes",
        "no_store_responses",
        "request_contracts_exact",
    }
    require(type(network) is dict and set(network) == expected_network, "browser.network", "shape")
    require(
        network["external_requests"] == 0
        and network["forbidden_queries"] == 0
        and network["console_errors"] == 0
        and network["page_errors"] == 0
        and network["login_statuses"] == [200, 200, 200, 200, 200]
        and network["logout_statuses"] == [200, 200, 200, 200, 200]
        and network["registration_statuses"] == [202]
        and network["refresh_statuses"] == [202]
        and network["server_statuses"] == [202, 202]
        and network["policy_statuses"] == [200]
        and type(network["list_successes"]) is int
        and network["list_successes"] >= 6
        and type(network["detail_successes"]) is int
        and network["detail_successes"] >= 2
        and network["no_store_responses"] is True
        and network["request_contracts_exact"] is True,
        "browser.network",
        "facts",
    )
    require(value["screenshots"] == list(SCREENSHOTS), "browser.complete", "screenshots")
    return value


def _validate_png(path: Path, width: int, height: int) -> None:
    require(path.is_file() and path.stat().st_size >= 1024, "browser.screenshot", "missing")
    try:
        with Image.open(path) as source:
            require(
                source.format == "PNG" and source.size == (width, height),
                "browser.screenshot",
                "dimensions",
            )
            source.verify()
        with Image.open(path) as source:
            colors = source.convert("RGB").getcolors(maxcolors=width * height)
    except (OSError, UnidentifiedImageError, ValueError):
        raise AcceptanceFailure("browser.screenshot", "decode") from None
    require(colors is not None and len(colors) >= 32, "browser.screenshot", "blank")
    dominant = max(count for count, _color in colors)
    require(dominant * 200 < width * height * 199, "browser.screenshot", "blank")


def _source_provenance() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in SOURCE_FILES.items():
        require(
            path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(ROOT),
            "evidence.source",
            "missing",
        )
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _assert_source_snapshot(expected: dict[str, str]) -> None:
    require(
        set(expected) == set(SOURCE_FILES) and _source_provenance() == expected,
        "evidence.source",
        "changed",
    )


class BrowserHarness:
    def __init__(self, run_id: str, secrets_: BrowserSecrets) -> None:
        self.run_id = RUNTIME.safe_run_id(run_id)
        self.secrets = secrets_
        self.owner = hashlib.sha256(secrets.token_bytes(48)).hexdigest()
        self.source_snapshot = _source_provenance()
        self.work, self.staging, self.final = create_run_directories(self.run_id, self.owner)
        self.processes = RUNTIME.OwnedProcesses()
        self.graph: BrowserGraph | None = None
        self.browser_facts: dict[str, object] | None = None
        self.private_extra: tuple[str, ...] = ()
        self.backend_viewer_denied = False
        self.high_approval_created = False
        self.permissions_revoked = False
        self.private_registered = False
        self.server: ThreadingHTTPServer | None = None
        self.lock = threading.Lock()

    def prepare(self) -> None:
        self._assert_ports_available()
        self.graph = prepare_database(self.work, self.secrets.runtime)
        environment = RUNTIME.service_environment(self.work, self.graph, self.secrets.runtime)
        environment.update(
            {
                "METAHUB_APPROVAL_SCHEDULER_ENABLED": "false",
                "METAHUB_APPROVAL_OUTBOX_ENABLED": "false",
                "METAHUB_APPROVAL_WRITEBACK_ENABLED": "false",
                "METAHUB_MCP_ENABLED": "true",
                "METAHUB_MCP_ENCRYPTION_VERSION": "1",
                "METAHUB_MCP_ENCRYPTION_SECRET": self.secrets.mcp_encryption_secret,
                "METAHUB_MCP_HMAC_VERSION": "1",
                "METAHUB_MCP_HMAC_SECRET": self.secrets.mcp_hmac_secret,
                "METAHUB_MCP_EXECUTOR_MODE": "disabled",
                "METAHUB_MCP_TEST_LOOPBACK_ENABLED": "false",
                "METAHUB_MCP_SCHEDULER_ENABLED": "false",
            }
        )
        control = self.processes.add(
            RUNTIME._BASE.start_service(
                "control",
                "metahub_control.app:app",
                RUNTIME.CONTROL_PORT,
                work=self.work,
                environment=environment,
            )
        )
        RUNTIME._BASE.wait_health(
            f"http://{LOOPBACK}:{RUNTIME.CONTROL_PORT}/health",
            control,
            stage="health.control",
        )
        console = self.processes.add(
            RUNTIME._BASE.start_console(work=self.work, environment=environment)
        )
        RUNTIME._BASE.wait_health(
            f"http://{LOOPBACK}:{RUNTIME.CONSOLE_PORT}/",
            console,
            stage="health.console",
        )
        self._verify_backend_and_create_invocation()

    def _assert_ports_available(self) -> None:
        for port in (RUNTIME.CONTROL_PORT, RUNTIME.CONSOLE_PORT, HARNESS_PORT):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind((LOOPBACK, port))
            except OSError:
                raise AcceptanceFailure("config.port", f"unavailable_{port}") from None
            finally:
                probe.close()

    @staticmethod
    def _error_contract(response: httpx.Response) -> bool:
        try:
            payload = response.json()
        except ValueError:
            return False
        return (
            response.status_code == 403
            and payload.get("code") == "auth.permission_denied"
            and isinstance(payload.get("request_id"), str)
            and response.headers.get("cache-control", "").startswith("no-store")
        )

    def _verify_backend_and_create_invocation(self) -> None:
        assert self.graph is not None
        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{RUNTIME.CONTROL_PORT}",
            timeout=httpx.Timeout(20, connect=3),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            viewer_login = client.post(
                "/auth/login",
                json={
                    "tenant_slug": "default",
                    "email": VIEWER_EMAIL,
                    "password": self.secrets.runtime.role_password,
                },
            )
            require(viewer_login.status_code == 200, "http.viewer_login", "status")
            viewer_token = str(viewer_login.json().get("access_token", ""))
            viewer_headers = {"Authorization": f"Bearer {viewer_token}"}
            denied = (
                client.get("/mcp/servers", headers=viewer_headers),
                client.get("/mcp/tools", headers=viewer_headers),
                client.get("/mcp/invocations", headers=viewer_headers),
                client.patch(
                    f"/mcp/servers/{self.graph.server_id}",
                    headers={**viewer_headers, "Idempotency-Key": "denied_status_contract"},
                    json={"expected_revision": 2, "status": "disabled"},
                ),
            )
            require(
                all(self._error_contract(item) for item in denied), "http.viewer_rbac", "denial"
            )
            require(
                client.post("/auth/logout", headers=viewer_headers).status_code == 200,
                "http.viewer_logout",
                "status",
            )

            admin_login = client.post(
                "/auth/login",
                json={
                    "tenant_slug": "default",
                    "email": ADMIN_EMAIL,
                    "password": self.secrets.runtime.admin_password,
                },
            )
            require(admin_login.status_code == 200, "http.admin_login", "status")
            admin_token = str(admin_login.json().get("access_token", ""))
            invocation_key = f"mcp_browser_high_{secrets.token_urlsafe(24)}"
            invoked = client.post(
                f"/mcp/tools/{self.graph.high_tool_id}/invoke",
                headers={
                    "Authorization": f"Bearer {admin_token}",
                    "Idempotency-Key": invocation_key,
                },
                json={"arguments": {"payload": self.secrets.invocation_argument}},
            )
            require(invoked.status_code == 202, "http.high_invocation", "status")
            body = invoked.json()
            invocation = body.get("invocation")
            require(
                type(invocation) is dict
                and invocation.get("status") == "approval_pending"
                and invocation.get("risk_level") == "high"
                and invocation.get("approval_required") is True
                and isinstance(invocation.get("approval_task_id"), str)
                and invoked.headers.get("cache-control", "").startswith("no-store"),
                "http.high_invocation",
                "contract",
            )
            require(
                self.secrets.invocation_argument.encode("utf-8") not in invoked.content,
                "http.high_invocation",
                "argument_disclosure",
            )
            require(
                client.post(
                    "/auth/logout", headers={"Authorization": f"Bearer {admin_token}"}
                ).status_code
                == 200,
                "http.admin_logout",
                "status",
            )
        self.private_extra = (viewer_token, admin_token, invocation_key)
        self.backend_viewer_denied = True
        self.high_approval_created = True

    def revoke_mcp_read(self) -> None:
        assert self.graph is not None
        with self.lock:
            require(not self.permissions_revoked, "browser.revoke", "already_applied")
            connection = connect_database(self.graph.database_url)
            try:
                connection.execute("BEGIN IMMEDIATE")
                role = connection.execute(
                    "SELECT permission_revision FROM roles "
                    "WHERE tenant_id=? AND role_id=? AND code='ops_admin'",
                    (self.graph.tenant_id, self.graph.revocable_role_id),
                ).fetchone()
                require(role is not None, "browser.revoke", "role")
                removed = connection.execute(
                    """
                    DELETE FROM role_permissions WHERE tenant_id=? AND role_id=?
                      AND permission_id=(SELECT permission_id FROM permissions
                        WHERE tenant_id=? AND code='mcp.read')
                    """,
                    (
                        self.graph.tenant_id,
                        self.graph.revocable_role_id,
                        self.graph.tenant_id,
                    ),
                ).rowcount
                require(removed == 1, "browser.revoke", "grant")
                retained = tuple(
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT permission.code FROM role_permissions grant_row
                        JOIN permissions permission ON permission.tenant_id=grant_row.tenant_id
                          AND permission.permission_id=grant_row.permission_id
                        WHERE grant_row.tenant_id=? AND grant_row.role_id=?
                        ORDER BY permission.code
                        """,
                        (self.graph.tenant_id, self.graph.revocable_role_id),
                    ).fetchall()
                )
                updated = connection.execute(
                    """
                    UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=?,
                      updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    WHERE tenant_id=? AND role_id=? AND permission_revision=?
                    """,
                    (
                        permission_matrix_sha256(retained),
                        self.graph.tenant_id,
                        self.graph.revocable_role_id,
                        role["permission_revision"],
                    ),
                ).rowcount
                require(updated == 1, "browser.revoke", "revision")
                connection.commit()
            except AcceptanceFailure:
                connection.rollback()
                raise
            except Exception:
                connection.rollback()
                raise AcceptanceFailure("browser.revoke", "database") from None
            finally:
                connection.close()
            self.permissions_revoked = True

    def register_private(self, payload: object) -> None:
        require(type(payload) is dict and set(payload) == {"values"}, "privacy.register", "shape")
        values = payload["values"]
        require(
            type(values) is list
            and 2 <= len(values) <= 32
            and all(
                type(item) is str
                and 8 <= len(item) <= 4096
                and all(33 <= ord(character) <= 126 for character in item)
                for item in values
            )
            and len(set(values)) == len(values)
            and not set(values).intersection(self.private_extra),
            "privacy.register",
            "values",
        )
        with self.lock:
            require(not self.private_registered, "privacy.register", "duplicate")
            self.private_extra = (*self.private_extra, *tuple(values))
            self.private_registered = True

    def serve(self) -> None:
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._reply(200, {"status": "ready", "all_108_prds_complete": False})
                else:
                    self._reply(404, {"status": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path not in {"/abort", "/complete", "/register-private", "/revoke-mcp"}:
                    self._reply(404, {"status": "not_found"})
                    return
                if not secrets.compare_digest(
                    self.headers.get("X-Harness-Token", ""), harness.secrets.stop_token
                ):
                    self._reply(403, {"status": "denied"})
                    return
                if self.path == "/abort":
                    self._reply(200, {"status": "aborting"})
                    assert harness.server is not None
                    harness.server.shutdown()
                    return
                try:
                    payload = self._read_json(
                        MAX_COMPLETE_BYTES if self.path == "/complete" else MAX_PRIVATE_BYTES
                    )
                    if self.path == "/register-private":
                        harness.register_private(payload)
                        self._reply(200, {"status": "accepted"})
                        return
                    if self.path == "/revoke-mcp":
                        require(payload == {}, "browser.revoke", "body")
                        harness.revoke_mcp_read()
                        self._reply(200, {"status": "revoked"})
                        return
                    require(harness.private_registered, "browser.complete", "private_values")
                    harness.browser_facts = validate_browser_facts(payload)
                    self._reply(200, {"status": "accepted"})
                    assert harness.server is not None
                    harness.server.shutdown()
                except (AcceptanceFailure, UnicodeError, ValueError, json.JSONDecodeError):
                    self._reply(409, {"status": "invalid"})

            def _read_json(self, maximum: int) -> object:
                lengths = self.headers.get_all("Content-Length", failobj=[])
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                    raise ValueError("length")
                length = int(lengths[0])
                if not 1 <= length <= maximum:
                    raise ValueError("length")
                return json.loads(self.rfile.read(length).decode("utf-8", errors="strict"))

            def _reply(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("ascii")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_arguments: object) -> None:
                return None

        self.server = ThreadingHTTPServer((LOOPBACK, HARNESS_PORT), Handler)
        self.server.daemon_threads = True
        try:
            self.server.serve_forever(poll_interval=0.1)
        finally:
            self.server.server_close()
            self.server = None

    def _database_summary(self) -> dict[str, object]:
        assert self.graph is not None
        connection = connect_database(self.graph.database_url)
        try:
            state = _migration_state(self.graph.database_url)
            server_count = int(connection.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0])
            tool_count = int(connection.execute("SELECT COUNT(*) FROM mcp_tools").fetchone()[0])
            high_tool_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM mcp_tools WHERE risk_level='high' AND approval_required=1"
                ).fetchone()[0]
            )
            invocation_count = int(
                connection.execute("SELECT COUNT(*) FROM mcp_invocations").fetchone()[0]
            )
            approval_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM mcp_invocations invocation
                    JOIN approval_tasks task
                      ON task.tenant_id=invocation.tenant_id
                     AND task.approval_task_id=invocation.approval_task_id
                    WHERE invocation.status='approval_pending'
                      AND invocation.approval_task_id IS NOT NULL
                      AND task.source_type='mcp'
                      AND task.source_id=invocation.mcp_invocation_id
                      AND task.target_type='mcp_tool_invocation'
                      AND task.target_id=invocation.mcp_invocation_id
                      AND task.target_action='invoke'
                      AND task.target_version='2:1:1:2'
                      AND task.target_fingerprint=invocation.policy_fingerprint
                      AND task.risk_level='high'
                      AND task.required_permission_code='mcp.approve'
                      AND task.created_by_type='user'
                      AND task.created_by_id=invocation.requested_by
                      AND task.mcp_invocation_id=invocation.mcp_invocation_id
                      AND task.source_request_id GLOB 'req_*'
                      AND task.source_trace_id GLOB 'trc_*'
                    """
                ).fetchone()[0]
            )
            approval_outbox_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM mcp_invocations invocation
                    JOIN approval_events event
                      ON event.tenant_id=invocation.tenant_id
                     AND event.approval_task_id=invocation.approval_task_id
                    JOIN approval_event_outbox outbox
                      ON outbox.tenant_id=event.tenant_id
                     AND outbox.approval_event_id=event.approval_event_id
                    WHERE invocation.status='approval_pending'
                      AND event.event_type='approval.created'
                      AND outbox.status='pending'
                    """
                ).fetchone()[0]
            )
            operation_states = {
                f"{row[0]}:{row[1]}": int(row[2])
                for row in connection.execute(
                    "SELECT operation_type,status,COUNT(*) FROM mcp_server_operations "
                    "GROUP BY operation_type,status ORDER BY operation_type,status"
                ).fetchall()
            }
            expected_operation_states = {
                "disable:succeeded": 1,
                "enable:succeeded": 1,
                "refresh:queued": 1,
                "register:queued": 1,
            }
            operation_count = sum(operation_states.values())
            mcp_audit_actions = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT action FROM audit_logs WHERE action LIKE 'mcp.%' ORDER BY action"
                ).fetchall()
            )
            read_grants = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM role_permissions grant_row
                    JOIN permissions permission ON permission.tenant_id=grant_row.tenant_id
                      AND permission.permission_id=grant_row.permission_id
                    WHERE grant_row.tenant_id=? AND grant_row.role_id=?
                      AND permission.code='mcp.read'
                    """,
                    (self.graph.tenant_id, self.graph.revocable_role_id),
                ).fetchone()[0]
            )
            require(
                state
                == {"count": 75, "version": "0075", "integrity": "ok", "foreign_key_violations": 0},
                "database.final",
                "migration",
            )
            require(server_count == 2 and tool_count == 2, "database.final", "catalog")
            require(high_tool_count == 2, "database.final", "high_policy")
            require(
                invocation_count == approval_count == approval_outbox_count == 1,
                "database.final",
                "invocation",
            )
            require(
                operation_states == expected_operation_states,
                "database.final",
                "operations",
            )
            require(
                mcp_audit_actions
                == (
                    "mcp.server_disabled",
                    "mcp.server_enabled",
                    "mcp.server_registered",
                    "mcp.tool_policy_updated",
                ),
                "database.final",
                "audit",
            )
            require(read_grants == 0 and self.permissions_revoked, "database.final", "revocation")
            require(
                connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                "database.final",
                "integrity",
            )
            require(
                not connection.execute("PRAGMA foreign_key_check").fetchall(),
                "database.final",
                "foreign_keys",
            )
        finally:
            connection.close()
        return {
            "migration": state,
            "server_count": server_count,
            "tool_count": tool_count,
            "high_approval_tool_count": high_tool_count,
            "invocation_count": invocation_count,
            "pending_approval_count": approval_count,
            "pending_approval_outbox_count": approval_outbox_count,
            "operation_count": operation_count,
            "operation_states": operation_states,
            "audit_count": len(mcp_audit_actions),
            "audit_actions": list(mcp_audit_actions),
            "ops_admin_read_grants_after_revocation": read_grants,
        }

    def publish(self) -> Path:
        require(
            self.graph is not None
            and self.browser_facts is not None
            and self.backend_viewer_denied
            and self.high_approval_created
            and self.permissions_revoked
            and self.private_registered,
            "browser",
            "incomplete",
        )
        _assert_source_snapshot(self.source_snapshot)
        self.processes.close()
        RUNTIME.quiesce_database(self.graph)
        dimensions = ((1280, 720), (1280, 720), (390, 844), (390, 844))
        for name, size in zip(SCREENSHOTS, dimensions, strict=True):
            _validate_png(self.staging / name, *size)
        private_values = self.secrets.values(self.private_extra)
        work_privacy = RUNTIME._BASE.verify_private_storage(self.work, private_values)
        staging_privacy = RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        sidecars = tuple(self.database_sidecars())
        require(not sidecars, "cleanup.sqlite", "sidecar")
        payload = {
            "prd_id": "PRD-082",
            "status": "passed",
            "run_id": self.run_id,
            "browser": self.browser_facts,
            "database": self._database_summary(),
            "privacy": {"work": work_privacy, "staging": staging_privacy},
            "processes": {
                "control": "owned_and_stopped",
                "console": "owned_and_stopped",
                "browser": "owned_and_stopped",
                "sqlite_sidecars": 0,
            },
            "source_provenance": {"algorithm": "sha256", **self.source_snapshot},
            "all_108_prds_complete": False,
        }
        RUNTIME.assert_safe_evidence(payload, private_values)
        evidence = self.staging / "browser-acceptance.json"
        evidence.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        entries = []
        for path in sorted(
            (item for item in self.staging.iterdir() if item.name != OWNER_FILENAME),
            key=lambda item: item.name,
        ):
            require(
                path.name in ALLOWED_ARTIFACTS - {"sha256-manifest.json"}
                and path.is_file()
                and not path.is_symlink(),
                "evidence.manifest",
                "entry",
            )
            entries.append(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        manifest = {"algorithm": "sha256", "files": entries, "all_108_prds_complete": False}
        (self.staging / "sha256-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        require(
            {item.name for item in self.staging.iterdir()} == ALLOWED_ARTIFACTS | {OWNER_FILENAME},
            "evidence.whitelist",
            "entries",
        )
        RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        _assert_source_snapshot(self.source_snapshot)
        os.replace(self.staging, self.final)
        require(
            {item.name for item in self.final.iterdir()} == ALLOWED_ARTIFACTS | {OWNER_FILENAME},
            "evidence.final",
            "entries",
        )
        RUNTIME._BASE.verify_private_storage(self.final, private_values)
        _assert_source_snapshot(self.source_snapshot)
        _remove_owned_directory(self.work, WORK_ROOT, self.owner, stage="cleanup.work")
        (self.final / OWNER_FILENAME).unlink()
        return self.final / "browser-acceptance.json"

    def database_sidecars(self) -> tuple[Path, ...]:
        assert self.graph is not None
        return tuple(
            path
            for path in (
                Path(f"{self.graph.database}-wal"),
                Path(f"{self.graph.database}-shm"),
                Path(f"{self.graph.database}-journal"),
            )
            if path.exists()
        )

    def abort(self) -> None:
        try:
            self.processes.close()
        finally:
            if self.graph is not None:
                try:
                    RUNTIME.quiesce_database(self.graph)
                except Exception:
                    pass
            for path, root, stage in (
                (self.final, ARTIFACT_ROOT, "cleanup.final"),
                (self.staging, ARTIFACT_ROOT, "cleanup.staging"),
                (self.work, WORK_ROOT, "cleanup.work"),
            ):
                if path.exists() and (path / OWNER_FILENAME).is_file():
                    _remove_owned_directory(path, root, self.owner, stage=stage)
            if self.server is not None:
                self.server.server_close()
                self.server = None


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(description="Serve PRD-082 PAGE-114 browser acceptance")
    parser.add_argument("--run-id", required=True)
    harness: BrowserHarness | None = None
    try:
        arguments = parser.parse_args(argv)
        harness = BrowserHarness(arguments.run_id, BrowserSecrets.from_environment())
        harness.prepare()
        print(
            json.dumps(
                {
                    "status": "ready",
                    "url": f"http://{LOOPBACK}:{RUNTIME.CONSOLE_PORT}/#mcp-tools",
                    "all_108_prds_complete": False,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        harness.serve()
        target = harness.publish()
        print(target.relative_to(ROOT).as_posix(), flush=True)
        return 0
    except AcceptanceFailure as exc:
        if harness is not None:
            harness.abort()
        print(
            f"PRD-082 browser harness failed: {exc.stage}:{exc.code}", file=sys.stderr, flush=True
        )
        return 1
    except Exception:
        if harness is not None:
            harness.abort()
        print("PRD-082 browser harness failed: harness:unexpected", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
