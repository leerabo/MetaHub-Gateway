"""Owner-safe PAGE-116 browser harness for PRD-084.

The harness owns a fresh 0077 SQLite database, the real Control and Console
processes, and one loopback OpenAI-compatible catalog stub. Formal evidence
contains only fixed states, counts, error codes, and source hashes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
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

ROOT = Path(__file__).resolve().parents[2]


def _load_base() -> Any:
    name = "_metahub_prd083_browser_for_prd084"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd083-browser-harness.py"
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
from metahub_shared.rbac import permission_matrix_sha256  # noqa: E402

WORK_ROOT = ROOT / ".tmp" / "prd084-browser-harness"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd084-browser"
LOOPBACK = "127.0.0.1"
HARNESS_PORT = 18887
SUCCESS_STUB_PORT = 18884
FAILURE_STUB_PORT = 18885
OWNER_FILENAME = ".prd084-browser-owner"
ADMIN_PASSWORD_ENV = "PRD084_BROWSER_ADMIN_PASSWORD"
ROLE_PASSWORD_ENV = "PRD084_BROWSER_ROLE_PASSWORD"
STOP_TOKEN_ENV = "PRD084_BROWSER_STOP_TOKEN"
EXPECTED_MIGRATION_COUNT = 77
EXPECTED_MIGRATION_VERSION = "0077"
PREVIOUS_MIGRATION_VERSION = "0076"
ADMIN_EMAIL = "admin@metahub.local"
DEVELOPER_EMAIL = "developer-prd084@local.invalid"
DEVELOPER_USER_ID = "usr_prd084_browser_developer"
SECONDARY_TENANT_ID = "tenant_prd084_browser_secondary"
SECONDARY_TENANT_SLUG = "prd084-secondary"
SECONDARY_EMAIL = "secondary-prd084@local.invalid"
SECONDARY_USER_ID = "usr_prd084_browser_secondary_admin"
SCREENSHOTS = (
    "page-116-create-dialog-desktop-1280x720.png",
    "page-116-local-endpoints-desktop-1280x720.png",
    "page-116-local-endpoints-mobile-390x844.png",
    "page-116-tenant-isolation-mobile-390x844.png",
)
ALLOWED_ARTIFACTS = frozenset((*SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json"))
DATABASE_PERSISTED_PRIVATE_LABELS = frozenset({"success_endpoint", "failure_endpoint"})
SOURCE_FILES = {
    "browser_harness": Path(__file__).resolve(),
    "browser_driver": ROOT / "scripts" / "acceptance" / "prd084-browser-acceptance.mjs",
    "browser_base_prd083": ROOT / "scripts" / "acceptance" / "prd083-browser-harness.py",
    "browser_helper_prd080": ROOT / "scripts" / "acceptance" / "prd080-browser-acceptance.mjs",
    "browser_helper_prd066": ROOT / "scripts" / "acceptance" / "prd066-browser-acceptance.mjs",
    "browser_helper_prd056": ROOT / "scripts" / "acceptance" / "prd056-browser-acceptance.mjs",
    "runtime_helper_prd078": ROOT / "scripts" / "acceptance" / "prd078-runtime-acceptance.py",
    "harness_contract": ROOT
    / "scripts"
    / "acceptance"
    / "tests"
    / "test_prd084_browser_harness.py",
    "driver_contract": ROOT / "console-web" / "tests" / "unit" / "prd084BrowserAcceptance.test.mjs",
    "migration_up": ROOT / "infra" / "migrations" / "0077_local_model_endpoint_access.sql",
    "migration_down": ROOT / "infra" / "migrations" / "0077_local_model_endpoint_access.down.sql",
    "console_main": ROOT / "console-web" / "src" / "app" / "main.js",
    "console_client": ROOT / "console-web" / "src" / "lib" / "localEndpoints.js",
    "console_state": ROOT
    / "console-web"
    / "src"
    / "features"
    / "local-models"
    / "localEndpointPageState.js",
    "console_binding": ROOT
    / "console-web"
    / "src"
    / "features"
    / "local-models"
    / "bindLocalEndpoints.js",
    "console_renderer": ROOT
    / "console-web"
    / "src"
    / "features"
    / "local-models"
    / "renderLocalEndpoints.js",
    "control_routes": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "local_endpoints"
    / "routes.py",
    "control_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "local_endpoints"
    / "service.py",
}


@dataclass(frozen=True, slots=True)
class BrowserSecrets:
    runtime: RUNTIME.RuntimeSecrets = field(repr=False)
    stop_token: str = field(repr=False)

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
            runtime=replace(generated, admin_password=admin_password, role_password=role_password),
            stop_token=stop_token,
        )

    def values(self, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        return self.runtime.values((self.stop_token, *extra))


@dataclass(frozen=True, slots=True)
class BrowserGraph:
    database: Path = field(repr=False)
    database_url: str = field(repr=False)
    tenant_id: str
    developer_role_id: str
    migration: dict[str, object]


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
    database = BASE.BASE._direct_child(work, "prd084-browser.sqlite3", stage="database.path")
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
        == {"count": 77, "version": "0077", "integrity": "ok", "foreign_key_violations": 0},
        "migration.fresh",
        "state",
    )
    repeat = apply_migrations(database_url)
    require(not repeat.applied and len(repeat.skipped) == 77, "migration.repeat", "result")
    repeat_state = _migration_state(database_url)
    down = rollback_last_migration(database_url, expected_version=EXPECTED_MIGRATION_VERSION)
    require(down.rolled_back == EXPECTED_MIGRATION_VERSION, "migration.down", "result")
    down_state = _migration_state(database_url)
    require(
        down_state
        == {"count": 76, "version": "0076", "integrity": "ok", "foreign_key_violations": 0},
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
        developer_role = connection.execute(
            "SELECT role_id FROM roles WHERE tenant_id=? AND code='developer'",
            (seed.tenant_id,),
        ).fetchone()
        require(developer_role is not None, "database.seed", "developer_role")
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,'developer','active',?,?)
            """,
            (
                DEVELOPER_USER_ID,
                seed.tenant_id,
                DEVELOPER_EMAIL,
                "PRD-084 Developer",
                hash_password(runtime.role_password),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,?,?,'internal_only','active',?,?)
            """,
            (SECONDARY_TENANT_ID, SECONDARY_TENANT_SLUG, "PRD-084 Secondary", now, now),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,'admin','active',?,?)
            """,
            (
                SECONDARY_USER_ID,
                SECONDARY_TENANT_ID,
                SECONDARY_EMAIL,
                "PRD-084 Secondary Admin",
                hash_password(runtime.admin_password),
                now,
                now,
            ),
        )
        connection.commit()
        grants = dict(
            connection.execute(
                """
                SELECT permission.code,COUNT(*) FROM role_permissions relation
                JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                  AND permission.permission_id=relation.permission_id
                WHERE relation.tenant_id=? AND relation.role_id=?
                  AND permission.code IN(
                    'local_endpoint.read','local_endpoint.test','local_endpoint.manage'
                  )
                GROUP BY permission.code
                """,
                (seed.tenant_id, developer_role["role_id"]),
            ).fetchall()
        )
        require(
            grants == {"local_endpoint.read": 1, "local_endpoint.test": 1},
            "database.seed",
            "developer_grants",
        )
        secondary_admin = connection.execute(
            "SELECT COUNT(*) FROM role_permissions relation JOIN roles role "
            "ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id "
            "JOIN permissions permission ON permission.tenant_id=relation.tenant_id "
            "AND permission.permission_id=relation.permission_id "
            "WHERE role.tenant_id=? AND role.code='admin' "
            "AND permission.code='local_endpoint.manage'",
            (SECONDARY_TENANT_ID,),
        ).fetchone()[0]
        require(int(secondary_admin) == 1, "database.seed", "secondary_admin")
        require(
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            and not connection.execute("PRAGMA foreign_key_check").fetchall(),
            "database.seed",
            "integrity",
        )
    finally:
        connection.close()
    return BrowserGraph(
        database=database,
        database_url=database_url,
        tenant_id=seed.tenant_id,
        developer_role_id=str(developer_role["role_id"]),
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
    "page_116",
    "endpoint_rows",
    "none_success",
    "bearer_failure",
    "native_dialog",
    "token_cleared",
    "url_cleared",
    "horizontal_overflow_free",
    "table_scroll_bounded",
    "text_overflow_free",
    "overlap_count",
    "no_sensitive_dom",
}
MOBILE_FACT_KEYS = {
    "width",
    "height",
    "nonblank",
    "page_116",
    "endpoint_rows",
    "bearer_failure",
    "tenant_isolation_empty",
    "horizontal_overflow_free",
    "table_scroll_bounded",
    "text_overflow_free",
    "overlap_count",
    "no_sensitive_dom",
}
LIFECYCLE_FACT_KEYS = {
    "backend_developer_manage_denied",
    "backend_secondary_empty",
    "developer_navigation_visible",
    "developer_read_test_only",
    "permission_drift_root_cleared",
    "permission_drift_navigation_cleared",
    "permission_drift_endpoint_dom_cleared",
    "late_response_discarded",
    "secondary_navigation_visible",
    "secondary_empty",
    "previous_tenant_dom_cleared",
    "registered_private_values",
    "logout_storage_cleared",
}


def _boolean_facts(value: object, expected: set[str], *, stage: str) -> dict[str, object]:
    require(type(value) is dict and set(value) == expected, stage, "shape")
    for name, item in value.items():
        if name not in {"width", "height", "overlap_count", "endpoint_rows"}:
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
    desktop = _boolean_facts(value["desktop"], DESKTOP_FACT_KEYS, stage="browser.desktop")
    mobile = _boolean_facts(value["mobile"], MOBILE_FACT_KEYS, stage="browser.mobile")
    require(
        (desktop["width"], desktop["height"], desktop["overlap_count"], desktop["endpoint_rows"])
        == (1280, 720, 0, 2),
        "browser.desktop",
        "layout",
    )
    require(
        (mobile["width"], mobile["height"], mobile["overlap_count"], mobile["endpoint_rows"])
        == (390, 844, 0, 2),
        "browser.mobile",
        "layout",
    )
    _boolean_facts(value["lifecycle"], LIFECYCLE_FACT_KEYS, stage="browser.lifecycle")
    network = value["network"]
    require(
        type(network) is dict
        and set(network)
        == {
            "external_requests",
            "forbidden_queries",
            "console_errors",
            "page_errors",
            "login_statuses",
            "logout_statuses",
            "create_statuses",
            "test_statuses",
            "list_successes",
            "no_store_responses",
            "request_contracts_exact",
        },
        "browser.network",
        "shape",
    )
    require(
        network["external_requests"] == network["forbidden_queries"] == 0
        and network["console_errors"] == network["page_errors"] == 0
        and network["login_statuses"] == [200, 200, 200]
        and network["logout_statuses"] == [200, 200, 200]
        and network["create_statuses"] == [201, 201]
        and network["test_statuses"] == [200, 200, 200]
        and type(network["list_successes"]) is int
        and network["list_successes"] >= 3
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


def _endpoint_summary_rows(connection: Any) -> list[Any]:
    return connection.execute(
        """
        SELECT provider.auth_type,provider.health_status,
          (SELECT COUNT(*) FROM local_model_catalog_entries catalog
           WHERE catalog.tenant_id=endpoint.tenant_id
             AND catalog.local_endpoint_id=endpoint.local_endpoint_id
             AND catalog.availability_status='available') AS discovered_model_count,
          endpoint.tenant_id
        FROM local_model_endpoints endpoint
        JOIN providers provider ON provider.tenant_id=endpoint.tenant_id
          AND provider.provider_id=endpoint.provider_id
        ORDER BY endpoint.created_at
        """
    ).fetchall()


def _provider_audit_rows(connection: Any, tenant_id: str) -> list[Any]:
    return connection.execute(
        "SELECT object_name,after_summary_json FROM audit_logs "
        "WHERE tenant_id=? AND action='provider.created' AND object_type='provider'",
        (tenant_id,),
    ).fetchall()


# The owner helpers are defined by the PRD-082 base and resolve module globals
# at call time. Bind those globals to this harness before exposing the helpers.
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
    setattr(BASE.BASE, _name, _value)


create_run_directories = BASE.create_run_directories
_remove_owned_directory = BASE._remove_owned_directory


class CatalogStub:
    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.models_calls = 0

    def start(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/v1/models" or self.headers.get("Authorization"):
                    self._reply(401, {"error": {"code": "unauthorized"}})
                    return
                owner.models_calls += 1
                self._reply(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {"id": "browser-chat", "object": "model"},
                            {"id": "browser-embed", "object": "model"},
                        ],
                    },
                )

            def _reply(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("ascii")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_arguments: object) -> None:
                return None

        self.server = ThreadingHTTPServer((LOOPBACK, SUCCESS_STUB_PORT), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None


class BrowserHarness:
    def __init__(self, run_id: str, secrets_: BrowserSecrets) -> None:
        self.run_id = RUNTIME.safe_run_id(run_id)
        self.secrets = secrets_
        self.owner = hashlib.sha256(secrets.token_bytes(48)).hexdigest()
        self.source_snapshot = _source_provenance()
        self.work, self.staging, self.final = create_run_directories(self.run_id, self.owner)
        self.processes = RUNTIME.OwnedProcesses()
        self.graph: BrowserGraph | None = None
        self.stub = CatalogStub()
        self.browser_facts: dict[str, object] | None = None
        self.private_extra: tuple[str, ...] = ()
        self.private_extra_labels: tuple[str, ...] = ()
        self.backend_developer_manage_denied = False
        self.backend_secondary_empty = False
        self.permissions_revoked = False
        self.private_registered = False
        self.server: ThreadingHTTPServer | None = None
        self.lock = threading.Lock()

    def _assert_ports_available(self) -> None:
        for port in (
            RUNTIME.CONTROL_PORT,
            RUNTIME.CONSOLE_PORT,
            HARNESS_PORT,
            SUCCESS_STUB_PORT,
            FAILURE_STUB_PORT,
        ):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind((LOOPBACK, port))
            except OSError:
                raise AcceptanceFailure("config.port", f"unavailable_{port}") from None
            finally:
                probe.close()

    def prepare(self) -> None:
        self._assert_ports_available()
        self.graph = prepare_database(self.work, self.secrets.runtime)
        environment = RUNTIME.service_environment(self.work, self.graph, self.secrets.runtime)
        environment.update(
            {
                "METAHUB_APPROVAL_SCHEDULER_ENABLED": "false",
                "METAHUB_APPROVAL_OUTBOX_ENABLED": "false",
                "METAHUB_APPROVAL_WRITEBACK_ENABLED": "false",
                "METAHUB_MCP_ENABLED": "false",
                "METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK": "true",
                "METAHUB_LOCAL_ENDPOINT_ALLOWED_PORTS": f"{SUCCESS_STUB_PORT},{FAILURE_STUB_PORT}",
                "METAHUB_LOCAL_ENDPOINT_PRIVATE_CIDRS": "",
                "METAHUB_LOCAL_ENDPOINT_CONNECT_TIMEOUT_SECONDS": "2",
                "METAHUB_LOCAL_ENDPOINT_OVERALL_TIMEOUT_SECONDS": "5",
                "METAHUB_SESSION_TTL_SECONDS": "3600",
            }
        )
        self.stub.start()
        control = self.processes.add(
            RUNTIME._BASE.start_process(
                "control",
                [
                    str(RUNTIME._BASE.PYTHON),
                    "-m",
                    "uvicorn",
                    "metahub_control.app:app",
                    "--host",
                    LOOPBACK,
                    "--port",
                    str(RUNTIME.CONTROL_PORT),
                    "--no-access-log",
                    "--log-level",
                    "error",
                    "--lifespan",
                    "off",
                ],
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

    def _login(self, client: httpx.Client, tenant: str, email: str, password: str) -> str:
        response = client.post(
            "/auth/login", json={"tenant_slug": tenant, "email": email, "password": password}
        )
        require(response.status_code == 200, "http.login", "status")
        token = str(response.json().get("access_token", ""))
        require(len(token) >= 32, "http.login", "token")
        return token

    def _verify_backend_rbac(self) -> None:
        private: list[str] = []
        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{RUNTIME.CONTROL_PORT}",
            timeout=httpx.Timeout(20, connect=3),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            developer = self._login(
                client, "default", DEVELOPER_EMAIL, self.secrets.runtime.role_password
            )
            developer_headers = {"Authorization": f"Bearer {developer}"}
            listed = client.get("/local-endpoints", headers=developer_headers)
            denied = client.post("/local-endpoints", headers=developer_headers, json={})
            navigation = client.get(
                "/console/navigation?active_item_id=local-models", headers=developer_headers
            )
            require(
                listed.status_code == 200
                and listed.json().get("local_endpoints") == []
                and navigation.status_code == 200
                and navigation.json().get("active_navigation_item_id") == "local-models"
                and self._error_contract(denied),
                "http.developer",
                "rbac",
            )
            require(
                client.post("/auth/logout", headers=developer_headers).status_code == 200,
                "http.developer",
                "logout",
            )
            secondary = self._login(
                client, SECONDARY_TENANT_SLUG, SECONDARY_EMAIL, self.secrets.runtime.admin_password
            )
            secondary_headers = {"Authorization": f"Bearer {secondary}"}
            secondary_list = client.get("/local-endpoints", headers=secondary_headers)
            require(
                secondary_list.status_code == 200
                and secondary_list.json().get("local_endpoints") == [],
                "http.secondary",
                "empty",
            )
            require(
                client.post("/auth/logout", headers=secondary_headers).status_code == 200,
                "http.secondary",
                "logout",
            )
            private.extend((developer, secondary))
        self.private_extra = tuple(private)
        self.private_extra_labels = ("backend_session", "backend_session")
        self.backend_developer_manage_denied = True
        self.backend_secondary_empty = True

    def revoke_local_read(self) -> None:
        assert self.graph is not None
        with self.lock:
            require(not self.permissions_revoked, "browser.revoke", "already_applied")
            connection = connect_database(self.graph.database_url)
            try:
                connection.execute("BEGIN IMMEDIATE")
                role = connection.execute(
                    "SELECT permission_revision FROM roles "
                    "WHERE tenant_id=? AND role_id=? AND code='developer'",
                    (self.graph.tenant_id, self.graph.developer_role_id),
                ).fetchone()
                require(role is not None, "browser.revoke", "role")
                removed = connection.execute(
                    """
                    DELETE FROM role_permissions WHERE tenant_id=? AND role_id=?
                      AND permission_id=(SELECT permission_id FROM permissions
                        WHERE tenant_id=? AND code='local_endpoint.read')
                    """,
                    (self.graph.tenant_id, self.graph.developer_role_id, self.graph.tenant_id),
                ).rowcount
                require(removed == 1, "browser.revoke", "grant")
                retained = tuple(
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT permission.code FROM role_permissions relation
                        JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                          AND permission.permission_id=relation.permission_id
                        WHERE relation.tenant_id=? AND relation.role_id=? ORDER BY permission.code
                        """,
                        (self.graph.tenant_id, self.graph.developer_role_id),
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
                        self.graph.developer_role_id,
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
            and 5 <= len(values) <= 32
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
            labels = (
                "driver_owner",
                "bearer_credential",
                "success_endpoint",
                "failure_endpoint",
                *("browser_session" for _item in values[4:]),
            )
            require(len(labels) == len(values), "privacy.register", "labels")
            self.private_extra_labels = (*self.private_extra_labels, *labels)
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
                if self.path not in {
                    "/abort",
                    "/complete",
                    "/register-private",
                    "/revoke-local-read",
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
                        BASE.BASE.MAX_COMPLETE_BYTES
                        if self.path == "/complete"
                        else BASE.BASE.MAX_PRIVATE_BYTES
                    )
                    if self.path == "/register-private":
                        harness.register_private(payload)
                    elif self.path == "/revoke-local-read":
                        require(payload == {}, "browser.revoke", "body")
                        harness.revoke_local_read()
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
            endpoints = _endpoint_summary_rows(connection)
            credential_rows = int(
                connection.execute("SELECT COUNT(*) FROM provider_credentials").fetchone()[0]
            )
            secondary_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM local_model_endpoints WHERE tenant_id=?",
                    (SECONDARY_TENANT_ID,),
                ).fetchone()[0]
            )
            read_grants = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM role_permissions relation
                    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                    WHERE relation.tenant_id=? AND relation.role_id=?
                      AND permission.code='local_endpoint.read'
                    """,
                    (self.graph.tenant_id, self.graph.developer_role_id),
                ).fetchone()[0]
            )
            audits = _provider_audit_rows(connection, self.graph.tenant_id)
            require(
                state
                == {"count": 77, "version": "0077", "integrity": "ok", "foreign_key_violations": 0},
                "database.final",
                "migration",
            )
            require(
                len(endpoints) == 2
                and {row["auth_type"] for row in endpoints} == {"none", "bearer"}
                and {row["health_status"] for row in endpoints} == {"healthy", "unhealthy"}
                and sorted(int(row["discovered_model_count"]) for row in endpoints) == [0, 2]
                and all(row["tenant_id"] == self.graph.tenant_id for row in endpoints),
                "database.final",
                "endpoints",
            )
            require(
                credential_rows >= 2 and secondary_rows == 0 and read_grants == 0,
                "database.final",
                "isolation",
            )
            require(
                len(audits) == 2
                and all(row["object_name"] in {"browser_none", "browser_bearer"} for row in audits)
                and all("Browser " not in str(row["after_summary_json"]) for row in audits),
                "database.final",
                "audit_privacy",
            )
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
            "local_endpoint_rows": len(endpoints),
            "none_rows": sum(1 for row in endpoints if row["auth_type"] == "none"),
            "bearer_rows": sum(1 for row in endpoints if row["auth_type"] == "bearer"),
            "healthy_rows": sum(1 for row in endpoints if row["health_status"] == "healthy"),
            "unhealthy_rows": sum(1 for row in endpoints if row["health_status"] == "unhealthy"),
            "secondary_tenant_rows": secondary_rows,
            "developer_read_grants_after_revocation": read_grants,
            "provider_create_audit_rows": len(audits),
            "catalog_stub_calls": self.stub.models_calls,
        }

    def publish(self) -> Path:
        require(
            self.graph is not None
            and self.browser_facts is not None
            and self.backend_developer_manage_denied
            and self.backend_secondary_empty
            and self.permissions_revoked
            and self.private_registered,
            "browser",
            "incomplete",
        )
        _assert_source_snapshot(self.source_snapshot)
        self.processes.close()
        self.stub.stop()
        RUNTIME.quiesce_database(self.graph)
        for name, size in zip(
            SCREENSHOTS, ((1280, 720), (1280, 720), (390, 844), (390, 844)), strict=True
        ):
            BASE.BASE._validate_png(self.staging / name, *size)
        private_values = self._private_values_for_scope("staging")
        work_private_values = self._private_values_for_scope("work")
        self._diagnose_private_storage(self.work, "work")
        self._diagnose_private_storage(self.staging, "staging")
        work_privacy = RUNTIME._BASE.verify_private_storage(self.work, work_private_values)
        staging_privacy = RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        require(not self.database_sidecars(), "cleanup.sqlite", "sidecar")
        payload = {
            "prd_id": "PRD-084",
            "status": "passed",
            "run_id": self.run_id,
            "browser": self.browser_facts,
            "database": self._database_summary(),
            "privacy": {"work": work_privacy, "staging": staging_privacy},
            "processes": {
                "control": "owned_and_stopped",
                "console": "owned_and_stopped",
                "catalog_stub": "owned_and_stopped",
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
        RUNTIME._BASE.verify_private_storage(self.final, private_values)
        _remove_owned_directory(self.work, WORK_ROOT, self.owner, stage="cleanup.work")
        (self.final / OWNER_FILENAME).unlink()
        return self.final / "browser-acceptance.json"

    def _private_values_for_scope(self, scope: str) -> tuple[str, ...]:
        """Keep persisted endpoint URLs out of the work-database secret set only."""

        require(scope in {"work", "staging"}, "privacy.scope", "invalid")
        extras = tuple(
            value
            for label, value in zip(self.private_extra_labels, self.private_extra, strict=True)
            if scope != "work" or label not in DATABASE_PERSISTED_PRIVATE_LABELS
        )
        return self.secrets.values(extras)

    def _diagnose_private_storage(self, root: Path, scope: str) -> None:
        require(scope in {"work", "staging"}, "privacy.diagnostic", "scope")
        runtime_values = self.secrets.runtime.values()
        labeled = (
            *(("runtime", value) for value in runtime_values),
            ("stop", self.secrets.stop_token),
            *zip(self.private_extra_labels, self.private_extra, strict=True),
        )
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            content = path.read_bytes()
            suffix = path.suffix.casefold()
            category = (
                "database"
                if suffix in {".db", ".sqlite", ".sqlite3"}
                else "log"
                if suffix == ".log"
                else "png"
                if suffix == ".png"
                else "json"
                if suffix == ".json"
                else "other"
            )
            for label, value in labeled:
                if (
                    scope == "work"
                    and category == "database"
                    and label in DATABASE_PERSISTED_PRIVATE_LABELS
                ):
                    # Local Endpoint URLs are required Provider configuration at rest.
                    # They remain forbidden in logs, browser storage and final evidence.
                    continue
                if any(
                    encoded in content
                    for encoded in RUNTIME._BASE.sensitive_encodings(value)
                ):
                    raise AcceptanceFailure(
                        "privacy.diagnostic", f"{scope}.{category}.{label}"
                    )

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
            self.stub.stop()
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
    parser = SafeArgumentParser(description="Serve PRD-084 PAGE-116 browser acceptance")
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
                    "url": f"http://{LOOPBACK}:{RUNTIME.CONSOLE_PORT}/#local-models",
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
            f"PRD-084 browser harness failed: {exc.stage}:{exc.code}", file=sys.stderr, flush=True
        )
        return 1
    except Exception as exc:
        if harness is not None:
            harness.abort()
        traceback = exc.__traceback__
        while traceback is not None and traceback.tb_next is not None:
            traceback = traceback.tb_next
        line = traceback.tb_lineno if traceback is not None else 0
        code = f"{type(exc).__name__.lower()}_l{line}"
        print(
            f"PRD-084 browser harness failed: harness.unexpected:{code}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
