"""PRD-087 PAGE-119 浏览器 harness：拥有独立进程、SQLite 和证据目录边界。

Owner-safe real-browser harness for PRD-087 PAGE-119.

The harness owns a fresh SQLite database, real Control/Console processes, and
the final browser evidence directory. It publishes only fixed facts, hashes,
and screenshots; credentials, sessions, request bodies, and response bodies
remain private runtime values.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import sys
from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]


def _load_prd085() -> Any:
    name = "_metahub_prd085_browser_for_prd087"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd085-browser-harness.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("browser harness base unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


BASE = _load_prd085()
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

WORK_ROOT = ROOT / ".tmp" / "prd087-browser-harness"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd087-browser"
LOOPBACK = "127.0.0.1"
HARNESS_PORT = 18889
OWNER_FILENAME = ".prd087-browser-owner"
ADMIN_PASSWORD_ENV = "PRD087_BROWSER_ADMIN_PASSWORD"
ROLE_PASSWORD_ENV = "PRD087_BROWSER_ROLE_PASSWORD"
STOP_TOKEN_ENV = "PRD087_BROWSER_STOP_TOKEN"
EXPECTED_MIGRATION_COUNT = 80
EXPECTED_MIGRATION_VERSION = "0080"
PREVIOUS_MIGRATION_VERSION = "0079"
ADMIN_EMAIL = "admin@metahub.local"
VIEWER_EMAIL = "viewer-prd087@local.invalid"
VIEWER_USER_ID = "usr_prd087_browser_viewer"
DEVELOPER_EMAIL = "developer-prd087@local.invalid"
DEVELOPER_USER_ID = "usr_prd087_browser_developer"
SECONDARY_TENANT_ID = "tenant_prd087_browser_secondary"
SECONDARY_TENANT_SLUG = "prd087-secondary"
SECONDARY_EMAIL = "secondary-prd087@local.invalid"
SECONDARY_USER_ID = "usr_prd087_browser_secondary_admin"
PRIMARY_PROJECT_ID = "prj_08700000000000000000000000000001"
SECONDARY_PARTNER_ID = "par_08700000000000000000000000000002"
SECONDARY_PARTNER_CODE = "secondary-channel"
SCREENSHOTS = (
    "page-119-partner-setup-mobile-390x844.png",
    "page-119-partner-ready-desktop-1280x720.png",
    "page-119-partner-commission-wide-1440x900.png",
    "page-119-partner-tenant-isolation-mobile-390x844.png",
)
ALLOWED_ARTIFACTS = frozenset((*SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json"))
SOURCE_FILES = {
    "browser_harness": Path(__file__).resolve(),
    "browser_driver": ROOT / "scripts" / "acceptance" / "prd087-browser-acceptance.mjs",
    "browser_base_prd085": ROOT / "scripts" / "acceptance" / "prd085-browser-harness.py",
    "browser_helper_prd066": ROOT / "scripts" / "acceptance" / "prd066-browser-acceptance.mjs",
    "runtime_helper_prd078": ROOT / "scripts" / "acceptance" / "prd078-runtime-acceptance.py",
    "harness_contract": ROOT
    / "scripts"
    / "acceptance"
    / "tests"
    / "test_prd087_browser_harness.py",
    "driver_contract": ROOT
    / "console-web"
    / "tests"
    / "unit"
    / "prd087BrowserAcceptanceDriver.test.mjs",
    "migration_up": ROOT / "infra" / "migrations" / "0080_channel_partner_white_label_portal.sql",
    "migration_down": ROOT
    / "infra"
    / "migrations"
    / "0080_channel_partner_white_label_portal.down.sql",
    "console_main": ROOT / "console-web" / "src" / "app" / "main.js",
    "console_styles": ROOT / "console-web" / "src" / "styles" / "main.css",
    "console_permissions": ROOT / "console-web" / "src" / "lib" / "permissions.js",
    "console_auth": ROOT / "console-web" / "src" / "lib" / "auth.js",
    "console_client": ROOT / "console-web" / "src" / "lib" / "partnerPortal.js",
    "console_state": ROOT
    / "console-web"
    / "src"
    / "features"
    / "partner-portal"
    / "partnerPortalPageState.js",
    "console_renderer": ROOT
    / "console-web"
    / "src"
    / "features"
    / "partner-portal"
    / "renderPartnerPortal.js",
    "console_binding": ROOT
    / "console-web"
    / "src"
    / "features"
    / "partner-portal"
    / "bindPartnerPortal.js",
    "control_routes": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "partner_portal"
    / "routes.py",
    "control_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "partner_portal"
    / "service.py",
    "control_models": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "partner_portal"
    / "models.py",
}

BrowserSecrets = BASE.BrowserSecrets
BrowserGraph = BASE.BrowserGraph


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


def _expected_state(count: int, version: str) -> dict[str, object]:
    return {"count": count, "version": version, "integrity": "ok", "foreign_key_violations": 0}


def prepare_database(work: Path, runtime: Any) -> BrowserGraph:
    """Verify 0080 fresh/repeat/down/re-up and seed isolated PAGE-119 identities."""
    database = BASE.BASE.BASE._direct_child(work, "prd087-browser.sqlite3", stage="database.path")
    database_url = _database_url(database)
    fresh = apply_migrations(database_url)
    require(
        len(fresh.applied) == EXPECTED_MIGRATION_COUNT
        and fresh.applied[-1:] == [EXPECTED_MIGRATION_VERSION],
        "migration.fresh",
        "result",
    )
    fresh_state = _migration_state(database_url)
    require(fresh_state == _expected_state(80, "0080"), "migration.fresh", "state")
    repeat = apply_migrations(database_url)
    require(not repeat.applied and len(repeat.skipped) == 80, "migration.repeat", "result")
    repeat_state = _migration_state(database_url)
    require(repeat_state == fresh_state, "migration.repeat", "state")
    down = rollback_last_migration(database_url, expected_version=EXPECTED_MIGRATION_VERSION)
    require(down.rolled_back == EXPECTED_MIGRATION_VERSION, "migration.down", "result")
    down_state = _migration_state(database_url)
    require(down_state == _expected_state(79, "0079"), "migration.down", "state")
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
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        roles = {
            str(row["code"]): str(row["role_id"])
            for row in connection.execute(
                "SELECT role_id,code FROM roles WHERE tenant_id=? "
                "AND code IN('admin','viewer','developer')",
                (seed.tenant_id,),
            ).fetchall()
        }
        require(set(roles) == {"admin", "viewer", "developer"}, "database.seed", "roles")
        admin_id = str(
            connection.execute(
                "SELECT user_id FROM users WHERE tenant_id=? AND email=?",
                (seed.tenant_id, ADMIN_EMAIL),
            ).fetchone()[0]
        )
        connection.executemany(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                (
                    VIEWER_USER_ID,
                    seed.tenant_id,
                    VIEWER_EMAIL,
                    "PRD-087 Viewer",
                    hash_password(runtime.role_password),
                    "viewer",
                    now,
                    now,
                ),
                (
                    DEVELOPER_USER_ID,
                    seed.tenant_id,
                    DEVELOPER_EMAIL,
                    "PRD-087 Developer",
                    hash_password(runtime.role_password),
                    "developer",
                    now,
                    now,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO projects(project_id,tenant_id,project_code,name,owner_user_id,status,
              created_by,updated_by,created_at,updated_at)
            VALUES(?,?,?,'Primary Partner Project',?,'active',?,?,?,?)
            """,
            (
                PRIMARY_PROJECT_ID,
                seed.tenant_id,
                "partner-primary",
                admin_id,
                admin_id,
                admin_id,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,?,?,'internal_only','active',?,?)
            """,
            (SECONDARY_TENANT_ID, SECONDARY_TENANT_SLUG, "PRD-087 Secondary", now, now),
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
                "PRD-087 Secondary Admin",
                hash_password(runtime.admin_password),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO partners(partner_id,tenant_id,partner_code,display_name,portal_name,
              brand_primary_color,support_text,default_commission_rate_bps,status,revision,
              idempotency_key_sha256,payload_sha256,created_by_user_id,
              operation_request_id,operation_trace_id,created_at,updated_at)
            VALUES(?,?,?,?,?,'#2364AA','Secondary tenant support',1000,'active',1,?,?,?,
              'req_prd087_secondary','trc_prd087_secondary',?,?)
            """,
            (
                SECONDARY_PARTNER_ID,
                SECONDARY_TENANT_ID,
                SECONDARY_PARTNER_CODE,
                "Secondary Channel",
                "Secondary Partner Workspace",
                "a" * 64,
                "b" * 64,
                SECONDARY_USER_ID,
                now,
                now,
            ),
        )
        connection.commit()
        grants = dict(
            connection.execute(
                """
                SELECT role.code,COUNT(permission.permission_id) FROM roles role
                LEFT JOIN role_permissions relation ON relation.tenant_id=role.tenant_id
                  AND relation.role_id=role.role_id
                LEFT JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                  AND permission.permission_id=relation.permission_id
                  AND permission.code='partner_portal.read'
                WHERE role.tenant_id=? AND role.code IN('admin','viewer','developer')
                GROUP BY role.code
                """,
                (seed.tenant_id,),
            ).fetchall()
        )
        secondary_grant = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM role_permissions relation
                JOIN roles role ON role.tenant_id=relation.tenant_id
                  AND role.role_id=relation.role_id
                JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                  AND permission.permission_id=relation.permission_id
                WHERE role.tenant_id=? AND role.code='admin'
                  AND permission.code='partner_portal.read'
                """,
                (SECONDARY_TENANT_ID,),
            ).fetchone()[0]
        )
        require(grants == {"admin": 1, "developer": 0, "viewer": 1}, "database.seed", "grants")
        require(secondary_grant == 1, "database.seed", "secondary")
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
        authorized_role_id=roles["viewer"],
        unauthorized_role_id=roles["developer"],
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
    "page_119",
    "ready_visible",
    "operations_visible",
    "horizontal_overflow_free",
    "content_bounded",
    "text_overflow_free",
    "overlap_count",
    "no_sensitive_dom",
}
MOBILE_FACT_KEYS = {
    "width",
    "height",
    "nonblank",
    "page_119",
    "setup_visible",
    "tenant_isolation_visible",
    "horizontal_overflow_free",
    "content_bounded",
    "text_overflow_free",
    "overlap_count",
    "no_sensitive_dom",
}
LIFECYCLE_FACT_KEYS = {
    "admin_initialization_visible",
    "admin_initialization_completed",
    "admin_ready_operations_visible",
    "viewer_navigation_visible",
    "viewer_operation_entry_visible",
    "unauthorized_navigation_absent",
    "unauthorized_page_absent",
    "unauthorized_api_prefetch_absent",
    "backend_permission_denied",
    "dynamic_revocation_root_cleared",
    "dynamic_revocation_navigation_cleared",
    "dynamic_revocation_dom_cleared",
    "dynamic_revocation_branding_cleared",
    "late_response_discarded",
    "secondary_navigation_visible",
    "secondary_tenant_independent",
    "previous_tenant_dom_cleared",
    "registered_private_values",
    "logout_storage_cleared",
}
NETWORK_FACT_KEYS = {
    "external_requests",
    "forbidden_queries",
    "console_errors",
    "page_errors",
    "login_statuses",
    "logout_statuses",
    "partner_request_count",
    "partner_statuses",
    "no_store_responses",
    "request_contract_exact",
    "required_routes_observed",
    "no_response_body_capture",
}


def _boolean_facts(value: object, expected: set[str], *, stage: str) -> dict[str, object]:
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
    desktop = _boolean_facts(value["desktop"], DESKTOP_FACT_KEYS, stage="browser.desktop")
    mobile = _boolean_facts(value["mobile"], MOBILE_FACT_KEYS, stage="browser.mobile")
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
    _boolean_facts(value["lifecycle"], LIFECYCLE_FACT_KEYS, stage="browser.lifecycle")
    network = value["network"]
    require(type(network) is dict and set(network) == NETWORK_FACT_KEYS, "browser.network", "shape")
    require(
        network["external_requests"] == network["forbidden_queries"] == 0
        and network["console_errors"] == network["page_errors"] == 0
        and network["login_statuses"] == [200, 200, 200, 200]
        and network["logout_statuses"] == [200, 200, 200, 200]
        and type(network["partner_request_count"]) is int
        and network["partner_request_count"] >= 16
        and type(network["partner_statuses"]) is list
        and all(
            type(item) is int and item in {200, 201, 403, 404}
            for item in network["partner_statuses"]
        )
        and network["no_store_responses"] is True
        and network["request_contract_exact"] is True
        and network["required_routes_observed"] is True
        and network["no_response_body_capture"] is True,
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


for _name, _value in {
    "WORK_ROOT": WORK_ROOT,
    "ARTIFACT_ROOT": ARTIFACT_ROOT,
    "HARNESS_PORT": HARNESS_PORT,
    "OWNER_FILENAME": OWNER_FILENAME,
    "ADMIN_PASSWORD_ENV": ADMIN_PASSWORD_ENV,
    "ROLE_PASSWORD_ENV": ROLE_PASSWORD_ENV,
    "STOP_TOKEN_ENV": STOP_TOKEN_ENV,
    "SCREENSHOTS": SCREENSHOTS,
    "ALLOWED_ARTIFACTS": ALLOWED_ARTIFACTS,
    "SOURCE_FILES": SOURCE_FILES,
    "prepare_database": prepare_database,
    "validate_browser_facts": validate_browser_facts,
    "_source_provenance": _source_provenance,
    "_assert_source_snapshot": _assert_source_snapshot,
}.items():
    setattr(BASE, _name, _value)

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

# create_run_directories is defined by the PRD-082 owner layer, so its function
# globals must receive the PAGE-119 roots as well as the two wrapper modules.
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
    setattr(BASE.BASE.BASE, _name, _value)

create_run_directories = BASE.create_run_directories
_remove_owned_directory = BASE._remove_owned_directory


class BrowserHarness(BASE.BrowserHarness):
    """PAGE-119 specialization of the reviewed owner-safe harness."""

    def _verify_backend_boundaries(self) -> None:
        assert self.graph is not None
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
            denied = client.get("/partners/profile", headers=developer_headers)
            require(self._permission_error(denied), "http.developer", "permission")
            require(
                client.post("/auth/logout", headers=developer_headers).status_code == 200,
                "http.developer",
                "logout",
            )
            secondary = self._login(
                client, SECONDARY_TENANT_SLUG, SECONDARY_EMAIL, self.secrets.runtime.admin_password
            )
            secondary_headers = {"Authorization": f"Bearer {secondary}"}
            isolated = client.get("/partners/profile", headers=secondary_headers)
            try:
                payload = isolated.json()
            except ValueError:
                payload = {}
            require(
                isolated.status_code == 200
                and isolated.headers.get("cache-control", "").startswith("no-store")
                and payload.get("partner", {}).get("partner_code") == SECONDARY_PARTNER_CODE,
                "http.secondary",
                "isolation",
            )
            require(
                client.post("/auth/logout", headers=secondary_headers).status_code == 200,
                "http.secondary",
                "logout",
            )
            private.extend((developer, secondary))
        self.private_extra = tuple(private)
        self.private_extra_labels = ("backend_session", "backend_session")
        self.backend_permission_denied = True
        self.backend_secondary_isolated = True

    def _change_partner_read(self, *, restore: bool) -> None:
        assert self.graph is not None
        with self.lock:
            require(self.permissions_revoked is restore, "browser.permission", "state")
            connection = connect_database(self.graph.database_url)
            try:
                connection.execute("BEGIN IMMEDIATE")
                role = connection.execute(
                    "SELECT permission_revision FROM roles "
                    "WHERE tenant_id=? AND role_id=? AND code='viewer'",
                    (self.graph.tenant_id, self.graph.authorized_role_id),
                ).fetchone()
                require(role is not None, "browser.permission", "role")
                if restore:
                    changed = connection.execute(
                        """
                        INSERT INTO role_permissions(
                          role_permission_id,tenant_id,role_id,permission_id,
                          created_at,updated_at)
                        SELECT 'rperm_'||lower(hex(randomblob(16))),?,?,permission_id,
                          strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
                        FROM permissions WHERE tenant_id=? AND code='partner_portal.read'
                        """,
                        (self.graph.tenant_id, self.graph.authorized_role_id, self.graph.tenant_id),
                    ).rowcount
                else:
                    changed = connection.execute(
                        """
                        DELETE FROM role_permissions WHERE tenant_id=? AND role_id=?
                          AND permission_id=(SELECT permission_id FROM permissions
                            WHERE tenant_id=? AND code='partner_portal.read')
                        """,
                        (self.graph.tenant_id, self.graph.authorized_role_id, self.graph.tenant_id),
                    ).rowcount
                require(changed == 1, "browser.permission", "grant")
                retained = tuple(
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT permission.code FROM role_permissions relation
                        JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                          AND permission.permission_id=relation.permission_id
                        WHERE relation.tenant_id=? AND relation.role_id=? ORDER BY permission.code
                        """,
                        (self.graph.tenant_id, self.graph.authorized_role_id),
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
                        self.graph.authorized_role_id,
                        role["permission_revision"],
                    ),
                ).rowcount
                require(updated == 1, "browser.permission", "revision")
                connection.commit()
            except AcceptanceFailure:
                connection.rollback()
                raise
            except Exception:
                connection.rollback()
                raise AcceptanceFailure("browser.permission", "database") from None
            finally:
                connection.close()
            self.permissions_revoked = not restore
            if restore:
                self.permission_restore_count += 1
            else:
                self.permission_revocation_count += 1

    def revoke_partner_portal_read(self) -> None:
        self._change_partner_read(restore=False)

    def restore_partner_portal_read(self) -> None:
        self._change_partner_read(restore=True)

    def serve(self) -> None:
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._reply(200, {"status": "ready", "all_108_prds_complete": False})
                else:
                    self._reply(404, {"status": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                allowed = {
                    "/abort",
                    "/complete",
                    "/register-private",
                    "/revoke-partner-portal-read",
                    "/restore-partner-portal-read",
                }
                if self.path not in allowed:
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
                        BASE.BASE.BASE.MAX_COMPLETE_BYTES
                        if self.path == "/complete"
                        else BASE.BASE.BASE.MAX_PRIVATE_BYTES
                    )
                    if self.path == "/register-private":
                        harness.register_private(payload)
                    elif self.path == "/revoke-partner-portal-read":
                        require(payload == {}, "browser.revoke", "body")
                        harness.revoke_partner_portal_read()
                    elif self.path == "/restore-partner-portal-read":
                        require(payload == {}, "browser.restore", "body")
                        harness.restore_partner_portal_read()
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
            primary_partners = int(
                connection.execute(
                    "SELECT COUNT(*) FROM partners WHERE tenant_id=?", (self.graph.tenant_id,)
                ).fetchone()[0]
            )
            primary_customers = int(
                connection.execute(
                    "SELECT COUNT(*) FROM partner_customers WHERE tenant_id=?",
                    (self.graph.tenant_id,),
                ).fetchone()[0]
            )
            secondary_partners = int(
                connection.execute(
                    "SELECT COUNT(*) FROM partners WHERE tenant_id=?", (SECONDARY_TENANT_ID,)
                ).fetchone()[0]
            )
            grants = dict(
                connection.execute(
                    """
                    SELECT role.code,COUNT(permission.permission_id) FROM roles role
                    LEFT JOIN role_permissions relation
                      ON relation.tenant_id=role.tenant_id
                     AND relation.role_id=role.role_id
                    LEFT JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                      AND permission.code='partner_portal.read'
                    WHERE role.tenant_id=? AND role.code IN('viewer','developer') GROUP BY role.code
                    """,
                    (self.graph.tenant_id,),
                ).fetchall()
            )
            secondary_grant = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM role_permissions relation
                    JOIN roles role ON role.tenant_id=relation.tenant_id
                      AND role.role_id=relation.role_id
                    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                    WHERE role.tenant_id=? AND role.code='admin'
                      AND permission.code='partner_portal.read'
                    """,
                    (SECONDARY_TENANT_ID,),
                ).fetchone()[0]
            )
            require(state == _expected_state(80, "0080"), "database.final", "migration")
            require(
                primary_partners == primary_customers == secondary_partners == 1,
                "database.final",
                "partners",
            )
            require(
                grants == {"developer": 0, "viewer": 1} and secondary_grant == 1,
                "database.final",
                "rbac",
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
            "primary_partner_count": primary_partners,
            "primary_customer_count": primary_customers,
            "secondary_partner_count": secondary_partners,
            "viewer_read_grants": grants["viewer"],
            "unauthorized_read_grants": grants["developer"],
            "secondary_read_grants": secondary_grant,
            "permission_catalog_revision": 22,
        }

    def publish(self) -> Path:
        require(
            self.graph is not None
            and self.browser_facts is not None
            and self.backend_permission_denied
            and self.backend_secondary_isolated
            and not self.permissions_revoked
            and self.permission_revocation_count == 1
            and self.permission_restore_count == 1
            and self.session_invalidation_count == 0
            and self.private_registered,
            "browser",
            "incomplete",
        )
        _assert_source_snapshot(self.source_snapshot)
        self.processes.close()
        RUNTIME.quiesce_database(self.graph)
        for name, size in zip(
            SCREENSHOTS, ((390, 844), (1280, 720), (1440, 900), (390, 844)), strict=True
        ):
            BASE.BASE.BASE._validate_png(self.staging / name, *size)
        private_values = self._private_values_for_scope("staging")
        self._diagnose_private_storage(self.work, "work")
        self._diagnose_private_storage(self.staging, "staging")
        work_privacy = RUNTIME._BASE.verify_private_storage(self.work, private_values)
        staging_privacy = RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        require(not self.database_sidecars(), "cleanup.sqlite", "sidecar")
        payload = {
            "prd_id": "PRD-087",
            "page_id": "PAGE-119",
            "status": "passed",
            "run_id": self.run_id,
            "browser": self.browser_facts,
            "database": self._database_summary(),
            "lifecycle_operations": {
                "permission_revocations": self.permission_revocation_count,
                "permission_restores": self.permission_restore_count,
            },
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
        RUNTIME._BASE.verify_private_storage(self.final, private_values)
        _remove_owned_directory(self.work, WORK_ROOT, self.owner, stage="cleanup.work")
        (self.final / OWNER_FILENAME).unlink()
        return self.final / "browser-acceptance.json"


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(description="Serve PRD-087 PAGE-119 browser acceptance")
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
                    "url": f"http://{LOOPBACK}:{RUNTIME.CONSOLE_PORT}/#partner-portal",
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
            f"PRD-087 browser harness failed: {exc.stage}:{exc.code}", file=sys.stderr, flush=True
        )
        return 1
    except Exception as exc:
        if harness is not None:
            harness.abort()
        traceback = exc.__traceback__
        while traceback is not None and traceback.tb_next is not None:
            traceback = traceback.tb_next
        line = traceback.tb_lineno if traceback is not None else 0
        failure_kind = type(exc).__name__.lower()
        print(
            f"PRD-087 browser harness failed: harness.unexpected:{failure_kind}_l{line}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
