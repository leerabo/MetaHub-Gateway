"""Owner-safe PAGE-115 browser harness for PRD-083.

The harness reuses the reviewed PRD-082 process ownership primitives while
owning a fresh 0076 SQLite database and PAGE-115-specific RBAC fixtures.  It
never publishes endpoint, credential, token, prompt, response, or result data.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import sys
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]


def _load_base() -> Any:
    name = "_metahub_prd082_browser_for_prd083"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd082-browser-harness.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("browser harness base unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


BASE = _load_base()
RUNTIME = BASE.RUNTIME
AcceptanceFailure = BASE.AcceptanceFailure
SafeArgumentParser = BASE.SafeArgumentParser
require = BASE.require

from metahub_shared.db import (  # noqa: E402
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.security import hash_password  # noqa: E402

WORK_ROOT = ROOT / ".tmp" / "prd083-browser-harness"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd083-browser"
LOOPBACK = "127.0.0.1"
HARNESS_PORT = 18886
OWNER_FILENAME = ".prd083-browser-owner"
ADMIN_PASSWORD_ENV = "PRD083_BROWSER_ADMIN_PASSWORD"
ROLE_PASSWORD_ENV = "PRD083_BROWSER_ROLE_PASSWORD"
STOP_TOKEN_ENV = "PRD083_BROWSER_STOP_TOKEN"
EXPECTED_MIGRATION_COUNT = 76
EXPECTED_MIGRATION_VERSION = "0076"
PREVIOUS_MIGRATION_VERSION = "0075"
CONFLICT_BUMP_CALL_TIMEOUT_SECONDS = 23
ADMIN_EMAIL = "admin@metahub.local"
SECURITY_EMAIL = "security-prd083@local.invalid"
SECURITY_USER_ID = "usr_prd083_browser_security"
OPS_EMAIL = "ops-prd083@local.invalid"
OPS_USER_ID = "usr_prd083_browser_ops"
DEVELOPER_EMAIL = "developer-prd083@local.invalid"
DEVELOPER_USER_ID = "usr_prd083_browser_developer"
VIEWER_EMAIL = "viewer-prd083@local.invalid"
VIEWER_USER_ID = "usr_prd083_browser_viewer"
SCREENSHOTS = (
    "page-115-network-desktop-1280x720.png",
    "page-115-conflict-desktop-1280x720.png",
    "page-115-network-mobile-390x844.png",
    "page-115-dialog-mobile-390x844.png",
)
ALLOWED_ARTIFACTS = frozenset((*SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json"))
SOURCE_FILES = {
    "browser_harness": Path(__file__).resolve(),
    "browser_driver": ROOT / "scripts" / "acceptance" / "prd083-browser-acceptance.mjs",
    "browser_base_prd082": ROOT / "scripts" / "acceptance" / "prd082-browser-harness.py",
    "browser_helper_prd082": ROOT / "scripts" / "acceptance" / "prd082-browser-acceptance.mjs",
    "browser_helper_prd080": ROOT / "scripts" / "acceptance" / "prd080-browser-acceptance.mjs",
    "browser_helper_prd066": ROOT / "scripts" / "acceptance" / "prd066-browser-acceptance.mjs",
    "browser_helper_prd056": ROOT / "scripts" / "acceptance" / "prd056-browser-acceptance.mjs",
    "runtime_helper_prd078": ROOT / "scripts" / "acceptance" / "prd078-runtime-acceptance.py",
    "harness_contract": ROOT
    / "scripts"
    / "acceptance"
    / "tests"
    / "test_prd083_browser_harness.py",
    "driver_contract": ROOT / "console-web" / "tests" / "unit" / "prd083BrowserAcceptance.test.mjs",
    "migration_up": ROOT / "infra" / "migrations" / "0076_mcp_sandbox_network_acl.sql",
    "migration_down": ROOT / "infra" / "migrations" / "0076_mcp_sandbox_network_acl.down.sql",
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


@dataclass(frozen=True, slots=True)
class BrowserSecrets:
    runtime: RUNTIME.RuntimeSecrets = field(repr=False)
    stop_token: str = field(repr=False)
    mcp_encryption_secret: str = field(repr=False)
    mcp_hmac_secret: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> BrowserSecrets:
        admin_password = os.environ.get(ADMIN_PASSWORD_ENV, "")
        role_password = os.environ.get(ROLE_PASSWORD_ENV, "")
        stop_token = os.environ.get(STOP_TOKEN_ENV, "")
        require(len(admin_password) >= 32, "config.admin_password", "invalid")
        require(len(role_password) >= 32, "config.role_password", "invalid")
        require(len(stop_token) >= 43, "config.stop_token", "invalid")
        generated = RUNTIME.RuntimeSecrets.create()
        return cls(
            runtime=replace(
                generated,
                admin_password=admin_password,
                role_password=role_password,
            ),
            stop_token=stop_token,
            mcp_encryption_secret=f"Prd083-McpEncryption-{secrets.token_urlsafe(48)}",
            mcp_hmac_secret=f"Prd083-McpHmac-{secrets.token_urlsafe(48)}",
        )

    def values(self, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        return self.runtime.values(
            (self.stop_token, self.mcp_encryption_secret, self.mcp_hmac_secret, *extra)
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
    migration: dict[str, object]


def _digest(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def _id(prefix: str, marker: str) -> str:
    return prefix + _digest(marker)[:32]


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


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


def prepare_database(work: Path, runtime: RUNTIME.RuntimeSecrets) -> BrowserGraph:
    database = BASE._direct_child(work, "prd083-browser.sqlite3", stage="database.path")
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
            "count": 76,
            "version": "0076",
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
        "migration.fresh",
        "state",
    )
    repeat = apply_migrations(database_url)
    require(not repeat.applied and len(repeat.skipped) == 76, "migration.repeat", "result")
    repeat_state = _migration_state(database_url)
    down = rollback_last_migration(database_url, expected_version=EXPECTED_MIGRATION_VERSION)
    require(down.rolled_back == EXPECTED_MIGRATION_VERSION, "migration.down", "result")
    down_state = _migration_state(database_url)
    require(
        down_state
        == {
            "count": 75,
            "version": "0075",
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
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
            "SELECT user.user_id,role.role_id FROM users user "
            "JOIN roles role ON role.tenant_id=user.tenant_id AND role.code='admin' "
            "WHERE user.tenant_id=? AND user.email=?",
            (seed.tenant_id, ADMIN_EMAIL),
        ).fetchone()
        require(actor is not None, "database.seed", "admin")
        roles = {
            str(row["code"]): str(row["role_id"])
            for row in connection.execute(
                "SELECT code,role_id FROM roles WHERE tenant_id=? AND code IN "
                "('security_admin','ops_admin','developer','viewer')",
                (seed.tenant_id,),
            ).fetchall()
        }
        require(
            set(roles) == {"security_admin", "ops_admin", "developer", "viewer"},
            "database.seed",
            "roles",
        )
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for user_id, email, display_name, role in (
            (SECURITY_USER_ID, SECURITY_EMAIL, "PRD-083 Security", "security_admin"),
            (OPS_USER_ID, OPS_EMAIL, "PRD-083 Operations", "ops_admin"),
            (DEVELOPER_USER_ID, DEVELOPER_EMAIL, "PRD-083 Developer", "developer"),
            (VIEWER_USER_ID, VIEWER_EMAIL, "PRD-083 Viewer", "viewer"),
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
        server_id = _id("mcps_", "prd083-browser-network-server")
        credential_id = _id("mcpc_", "prd083-browser-network-credential")
        connection.execute(
            """
            INSERT INTO mcp_servers(
              mcp_server_id,tenant_id,name,transport,endpoint_ciphertext,
              endpoint_encryption_key_version,endpoint_hmac,endpoint_hmac_key_version,
              endpoint_scheme,endpoint_host_hint,endpoint_has_path,
              active_credential_revision,protocol_version,capabilities_sha256,status,
              state_revision,created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,'Network Security Server','streamable_http','opaque-endpoint',
                     1,?,1,'https','n***.invalid',1,NULL,NULL,NULL,'pending_sync',1,
                     ?,?,?,?)
            """,
            (
                server_id,
                seed.tenant_id,
                _digest("network-endpoint"),
                actor["user_id"],
                actor["user_id"],
                now,
                now,
            ),
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
            (credential_id, seed.tenant_id, server_id, actor["user_id"], now),
        )
        connection.execute(
            "UPDATE mcp_server_credentials SET status='active',activated_at=? "
            "WHERE tenant_id=? AND mcp_server_credential_id=?",
            (now, seed.tenant_id, credential_id),
        )
        connection.execute(
            "UPDATE mcp_servers SET active_credential_revision=1,protocol_version='2025-06-18',"
            "capabilities_sha256=?,status='active',state_revision=2,updated_at=? "
            "WHERE tenant_id=? AND mcp_server_id=?",
            (_digest("network-capabilities"), now, seed.tenant_id, server_id),
        )
        connection.commit()
        policy = connection.execute(
            """
            SELECT current_revision,status FROM mcp_network_policies
            WHERE tenant_id=? AND mcp_server_id=?
            """,
            (seed.tenant_id, server_id),
        ).fetchone()
        require(tuple(policy) == (1, "disabled"), "database.seed", "network_policy")
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
        admin_user_id=str(actor["user_id"]),
        admin_role_id=str(actor["role_id"]),
        revocable_role_id=roles["ops_admin"],
        server_id=server_id,
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
    "page_115",
    "network_summary_visible",
    "admin_updated",
    "security_updated",
    "ops_updated",
    "conflict_visible",
    "draft_preserved",
    "rebase_revision_only",
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
    "page_115",
    "network_summary_visible",
    "native_dialog",
    "bounded_fields",
    "horizontal_overflow_free",
    "text_overflow_free",
    "overlap_count",
    "no_sensitive_dom",
    "next_content_hint",
}
LIFECYCLE_FACT_KEYS = {
    "backend_viewer_denied",
    "backend_developer_write_denied",
    "developer_navigation_visible",
    "developer_read_only",
    "viewer_navigation_hidden",
    "viewer_page_hidden",
    "permission_drift_root_cleared",
    "permission_drift_navigation_cleared",
    "late_response_discarded",
    "registered_private_values",
    "logout_storage_cleared",
}


def _all_true(value: object, expected: set[str], *, stage: str) -> dict[str, object]:
    require(type(value) is dict and set(value) == expected, stage, "shape")
    for name, item in value.items():
        if name not in {"width", "height", "overlap_count"}:
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
        (desktop["width"], desktop["height"], desktop["overlap_count"]) == (1280, 720, 0),
        "browser.desktop",
        "layout",
    )
    require(
        (mobile["width"], mobile["height"], mobile["overlap_count"]) == (390, 844, 0),
        "browser.mobile",
        "layout",
    )
    _all_true(value["lifecycle"], LIFECYCLE_FACT_KEYS, stage="browser.lifecycle")
    network = value["network"]
    expected = {
        "external_requests",
        "forbidden_queries",
        "console_errors",
        "page_errors",
        "login_statuses",
        "logout_statuses",
        "policy_statuses",
        "list_successes",
        "no_store_responses",
        "request_contracts_exact",
    }
    require(type(network) is dict and set(network) == expected, "browser.network", "shape")
    require(
        network["external_requests"] == network["forbidden_queries"] == 0
        and network["console_errors"] == network["page_errors"] == 0
        and network["login_statuses"] == [200] * 6
        and network["logout_statuses"] == [200] * 6
        and network["policy_statuses"] == [200, 200, 409, 200]
        and type(network["list_successes"]) is int
        and network["list_successes"] >= 8
        and network["no_store_responses"] is True
        and network["request_contracts_exact"] is True,
        "browser.network",
        "facts",
    )
    require(value["screenshots"] == list(SCREENSHOTS), "browser.complete", "screenshots")
    return value


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


# The reused ownership helpers resolve these module globals at call time.
for _name, _value in {
    "WORK_ROOT": WORK_ROOT,
    "ARTIFACT_ROOT": ARTIFACT_ROOT,
    "HARNESS_PORT": HARNESS_PORT,
    "OWNER_FILENAME": OWNER_FILENAME,
    "SCREENSHOTS": SCREENSHOTS,
    "ALLOWED_ARTIFACTS": ALLOWED_ARTIFACTS,
    "SOURCE_FILES": SOURCE_FILES,
    "validate_browser_facts": validate_browser_facts,
    "_source_provenance": _source_provenance,
    "_assert_source_snapshot": _assert_source_snapshot,
}.items():
    setattr(BASE, _name, _value)

create_run_directories = BASE.create_run_directories
_remove_owned_directory = BASE._remove_owned_directory


class BrowserHarness(BASE.BrowserHarness):
    def __init__(self, run_id: str, secrets_: BrowserSecrets) -> None:
        super().__init__(run_id, secrets_)
        self.backend_developer_write_denied = False

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
            f"http://{LOOPBACK}:{RUNTIME.CONTROL_PORT}/health", control, stage="health.control"
        )
        console = self.processes.add(
            RUNTIME._BASE.start_console(work=self.work, environment=environment)
        )
        RUNTIME._BASE.wait_health(
            f"http://{LOOPBACK}:{RUNTIME.CONSOLE_PORT}/", console, stage="health.console"
        )
        self._verify_backend_rbac()

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

    def _login(self, client: httpx.Client, email: str, password: str) -> tuple[str, httpx.Response]:
        response = client.post(
            "/auth/login", json={"tenant_slug": "default", "email": email, "password": password}
        )
        require(response.status_code == 200, "http.login", "status")
        token = str(response.json().get("access_token", ""))
        require(len(token) >= 32, "http.login", "token")
        return token, response

    def _verify_backend_rbac(self) -> None:
        assert self.graph is not None
        private: list[str] = []
        body = {
            "expected_revision": 1,
            "status": "active",
            "egress_mode": "registered_endpoint_only",
            "connect_timeout_seconds": 5,
            "probe_timeout_seconds": 10,
            "call_timeout_seconds": 30,
            "failure_threshold": 3,
            "open_seconds": 60,
        }
        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{RUNTIME.CONTROL_PORT}",
            timeout=httpx.Timeout(20, connect=3),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            admin, _ = self._login(client, ADMIN_EMAIL, self.secrets.runtime.admin_password)
            admin_headers = {"Authorization": f"Bearer {admin}"}
            admin_me = client.get("/auth/me", headers=admin_headers)
            admin_navigation = client.get(
                "/console/navigation?active_item_id=mcp-tools", headers=admin_headers
            )
            require(admin_me.status_code == 200, "http.admin.me", "status")
            require(
                admin_navigation.status_code == 200,
                "http.admin.navigation",
                "status",
            )
            me_payload = admin_me.json()
            navigation_payload = admin_navigation.json()
            require(
                "mcp.read" in me_payload.get("permission_codes", [])
                and navigation_payload.get("active_navigation_item_id") == "mcp-tools"
                and any(
                    item.get("id") == "mcp-tools"
                    for item in navigation_payload.get("navigation_items", [])
                    if isinstance(item, dict)
                )
                and navigation_payload.get("permission_codes") == me_payload.get("permission_codes")
                and navigation_payload.get("permission_revision")
                == me_payload.get("permission_revision")
                and navigation_payload.get("authorization_fingerprint")
                == me_payload.get("authorization_fingerprint"),
                "http.admin.navigation",
                "navigation",
            )
            require(
                client.post("/auth/logout", headers=admin_headers).status_code == 200,
                "http.admin",
                "logout",
            )

            viewer, _ = self._login(client, VIEWER_EMAIL, self.secrets.runtime.role_password)
            viewer_headers = {"Authorization": f"Bearer {viewer}"}
            require(
                self._error_contract(client.get("/mcp/servers", headers=viewer_headers)),
                "http.viewer",
                "read",
            )
            require(
                self._error_contract(
                    client.patch(
                        f"/mcp/servers/{self.graph.server_id}/network-policy",
                        headers=viewer_headers,
                        json=body,
                    )
                ),
                "http.viewer",
                "write",
            )
            require(
                client.post("/auth/logout", headers=viewer_headers).status_code == 200,
                "http.viewer",
                "logout",
            )

            developer, _ = self._login(client, DEVELOPER_EMAIL, self.secrets.runtime.role_password)
            developer_headers = {"Authorization": f"Bearer {developer}"}
            listed = client.get("/mcp/servers", headers=developer_headers)
            require(
                listed.status_code == 200
                and listed.headers.get("cache-control", "").startswith("no-store"),
                "http.developer",
                "read",
            )
            require(
                self._error_contract(
                    client.patch(
                        f"/mcp/servers/{self.graph.server_id}/network-policy",
                        headers=developer_headers,
                        json=body,
                    )
                ),
                "http.developer",
                "write",
            )
            require(
                client.post("/auth/logout", headers=developer_headers).status_code == 200,
                "http.developer",
                "logout",
            )
            private.extend((admin, viewer, developer))
        self.private_extra = tuple(private)
        self.backend_viewer_denied = True
        self.backend_developer_write_denied = True
        self.high_approval_created = True

    def bump_network_policy(self) -> None:
        assert self.graph is not None
        with (
            self.lock,
            httpx.Client(
                base_url=f"http://{LOOPBACK}:{RUNTIME.CONTROL_PORT}",
                timeout=httpx.Timeout(20, connect=3),
                follow_redirects=False,
                trust_env=False,
            ) as client,
        ):
            token, _ = self._login(client, ADMIN_EMAIL, self.secrets.runtime.admin_password)
            with closing(connect_database(self.graph.database_url)) as connection:
                row = connection.execute(
                    """
                    SELECT policy_revision,status,egress_mode,connect_timeout_seconds,
                      probe_timeout_seconds,call_timeout_seconds,failure_threshold,
                      open_seconds
                    FROM mcp_network_policy_revisions
                    WHERE tenant_id=? AND mcp_server_id=?
                    ORDER BY policy_revision DESC LIMIT 1
                    """,
                    (self.graph.tenant_id, self.graph.server_id),
                ).fetchone()
            require(row is not None, "browser.bump", "policy")
            payload = {
                "expected_revision": int(row["policy_revision"]),
                "status": str(row["status"]),
                "egress_mode": str(row["egress_mode"]),
                "connect_timeout_seconds": int(row["connect_timeout_seconds"]),
                "probe_timeout_seconds": int(row["probe_timeout_seconds"]),
                "call_timeout_seconds": CONFLICT_BUMP_CALL_TIMEOUT_SECONDS,
                "failure_threshold": int(row["failure_threshold"]),
                "open_seconds": int(row["open_seconds"]),
            }
            response = client.patch(
                f"/mcp/servers/{self.graph.server_id}/network-policy",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            require(
                response.status_code == 200
                and response.json().get("network_policy", {}).get("revision")
                == int(row["policy_revision"]) + 1,
                "browser.bump",
                "status",
            )
            require(
                client.post(
                    "/auth/logout", headers={"Authorization": f"Bearer {token}"}
                ).status_code
                == 200,
                "browser.bump",
                "logout",
            )
            self.private_extra = (*self.private_extra, token)

    def serve(self) -> None:
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self._reply(
                    200, {"status": "ready", "all_108_prds_complete": False}
                ) if self.path == "/health" else self._reply(404, {"status": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path not in {
                    "/abort",
                    "/complete",
                    "/register-private",
                    "/revoke-mcp",
                    "/bump-network-policy",
                }:
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
                        BASE.MAX_COMPLETE_BYTES
                        if self.path == "/complete"
                        else BASE.MAX_PRIVATE_BYTES
                    )
                    if self.path == "/register-private":
                        harness.register_private(payload)
                    elif self.path == "/revoke-mcp":
                        require(payload == {}, "browser.revoke", "body")
                        harness.revoke_mcp_read()
                    elif self.path == "/bump-network-policy":
                        require(payload == {}, "browser.bump", "body")
                        harness.bump_network_policy()
                    else:
                        require(harness.private_registered, "browser.complete", "private_values")
                        harness.browser_facts = validate_browser_facts(payload)
                    self._reply(200, {"status": "accepted"})
                    if self.path == "/complete":
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
            current = connection.execute(
                """
                SELECT current_revision,status FROM mcp_network_policies
                WHERE tenant_id=? AND mcp_server_id=?
                """,
                (self.graph.tenant_id, self.graph.server_id),
            ).fetchone()
            revisions = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM mcp_network_policy_revisions
                    WHERE tenant_id=? AND mcp_server_id=?
                    """,
                    (self.graph.tenant_id, self.graph.server_id),
                ).fetchone()[0]
            )
            audits = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_logs
                    WHERE tenant_id=? AND action='mcp.network_policy_updated'
                    """,
                    (self.graph.tenant_id,),
                ).fetchone()[0]
            )
            read_grants = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM role_permissions relation
                    JOIN permissions permission
                      ON permission.tenant_id=relation.tenant_id
                     AND permission.permission_id=relation.permission_id
                    WHERE relation.tenant_id=? AND relation.role_id=?
                      AND permission.code='mcp.read'
                    """,
                    (self.graph.tenant_id, self.graph.revocable_role_id),
                ).fetchone()[0]
            )
            require(
                state
                == {"count": 76, "version": "0076", "integrity": "ok", "foreign_key_violations": 0},
                "database.final",
                "migration",
            )
            require(
                tuple(current) == (5, "active") and revisions == 5 and audits == 4,
                "database.final",
                "network_policy",
            )
            require(read_grants == 0 and self.permissions_revoked, "database.final", "revocation")
            require(
                connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                and not connection.execute("PRAGMA foreign_key_check").fetchall(),
                "database.final",
                "integrity",
            )
        finally:
            connection.close()
        return {
            "migration": state,
            "network_policy_revision": int(current["current_revision"]),
            "network_policy_status": str(current["status"]),
            "network_policy_revision_count": revisions,
            "network_policy_audit_count": audits,
            "ops_read_grants_after_revocation": read_grants,
        }

    def publish(self) -> Path:
        require(
            self.graph is not None
            and self.browser_facts is not None
            and self.backend_viewer_denied
            and self.backend_developer_write_denied
            and self.permissions_revoked
            and self.private_registered,
            "browser",
            "incomplete",
        )
        _assert_source_snapshot(self.source_snapshot)
        self.processes.close()
        RUNTIME.quiesce_database(self.graph)
        for name, size in zip(
            SCREENSHOTS, ((1280, 720), (1280, 720), (390, 844), (390, 844)), strict=True
        ):
            BASE._validate_png(self.staging / name, *size)
        private_values = self.secrets.values(self.private_extra)
        work_privacy = RUNTIME._BASE.verify_private_storage(self.work, private_values)
        staging_privacy = RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        require(not self.database_sidecars(), "cleanup.sqlite", "sidecar")
        payload = {
            "prd_id": "PRD-083",
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
        _remove_owned_directory(self.work, WORK_ROOT, self.owner, stage="cleanup.work")
        (self.final / OWNER_FILENAME).unlink()
        return self.final / "browser-acceptance.json"


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(description="Serve PRD-083 PAGE-115 browser acceptance")
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
            f"PRD-083 browser harness failed: {exc.stage}:{exc.code}", file=sys.stderr, flush=True
        )
        return 1
    except Exception:
        if harness is not None:
            harness.abort()
        print("PRD-083 browser harness failed: harness:unexpected", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
