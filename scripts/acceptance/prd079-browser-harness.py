"""Owner-safe loopback harness for PRD-079 PAGE-110/111 browser acceptance.

The harness owns one repository-local SQLite database and the exact service
processes needed by the browser driver. Credentials stay in process memory and
child environments. Published evidence is restricted to redacted JSON, four
PNG screenshots, and their SHA-256 manifest.
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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[2]
WORK_ROOT = ROOT / ".tmp" / "prd079-browser-harness"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd079-browser"
LOOPBACK = "127.0.0.1"
GATEWAY_PORT = 8000
HARNESS_PORT = 18882
OWNER_FILENAME = ".prd079-browser-owner"
ROLE_PASSWORD_ENV = "PRD079_BROWSER_ROLE_PASSWORD"
STOP_TOKEN_ENV = "PRD079_BROWSER_STOP_TOKEN"
MAX_COMPLETE_BYTES = 16 * 1024
EXPECTED_MIGRATION_COUNT = 72
EXPECTED_MIGRATION_VERSION = "0072"
FINANCE_EMAIL = "finance@prd079.runtime"
DEVELOPER_EMAIL = "developer@prd079.runtime"
SCREENSHOTS = (
    "page-110-plan-desktop-1280x720.png",
    "page-110-plan-mobile-390x844.png",
    "page-111-quota-desktop-1280x720.png",
    "page-111-quota-mobile-390x844.png",
)
ALLOWED_ARTIFACTS = frozenset(
    (*SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json")
)
SOURCE_FILES = {
    "browser_harness": Path(__file__).resolve(),
    "browser_acceptance": ROOT / "scripts" / "acceptance" / "prd079-browser-acceptance.mjs",
    "migration_up": ROOT / "infra" / "migrations" / "0072_plan_trial_quota_types.sql",
    "console_main": ROOT / "console-web" / "src" / "app" / "main.js",
    "plan_billing_state": ROOT
    / "console-web"
    / "src"
    / "features"
    / "plan-billing"
    / "planBillingPageState.js",
    "plan_billing_binding": ROOT
    / "console-web"
    / "src"
    / "features"
    / "plan-billing"
    / "bindPlanBilling.js",
    "plan_billing_render": ROOT
    / "console-web"
    / "src"
    / "features"
    / "plan-billing"
    / "renderPlanBilling.js",
    "plan_billing_styles": ROOT / "console-web" / "src" / "styles" / "main.css",
}


def _load_runtime_module() -> Any:
    """Load only the proven process/config helpers from PRD-078."""

    name = "_metahub_prd078_runtime_helpers_for_prd079_browser"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd078-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("browser harness dependency unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


RUNTIME = _load_runtime_module()
AcceptanceFailure = RUNTIME.AcceptanceFailure
SafeArgumentParser = RUNTIME.SafeArgumentParser
require = RUNTIME.require

from metahub_shared.db import apply_migrations, apply_seed_data, connect_database  # noqa: E402
from metahub_shared.db.security import hash_password  # noqa: E402
from metahub_shared.rbac import permission_matrix_sha256  # noqa: E402


@dataclass(frozen=True, slots=True)
class BrowserSecrets:
    """Keep all browser credentials out of repr and persistent evidence."""

    runtime: RUNTIME.RuntimeSecrets = field(repr=False)
    stop_token: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> BrowserSecrets:
        role_password = os.environ.get(ROLE_PASSWORD_ENV, "")
        stop_token = os.environ.get(STOP_TOKEN_ENV, "")
        require(len(role_password) >= 32, "config.role_password", "invalid")
        require(len(stop_token) >= 43, "config.stop_token", "invalid")
        generated = RUNTIME.RuntimeSecrets.create()
        return cls(runtime=replace(generated, role_password=role_password), stop_token=stop_token)

    def private_values(self, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        return self.runtime.values((self.stop_token, *extra))


@dataclass(frozen=True, slots=True)
class BrowserGraph:
    database: Path = field(repr=False)
    database_url: str = field(repr=False)
    tenant_id: str = field(repr=False)
    migration: dict[str, object]


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _db_time() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _direct_child(root: Path, name: str, *, stage: str) -> Path:
    require(root.resolve().is_relative_to(ROOT.resolve()), stage, "root")
    require(Path(name).name == name and name not in {".", ".."}, stage, "name")
    target = root / name
    require(target.parent.resolve() == root.resolve(), stage, "boundary")
    return target


def create_run_directories(run_id: str, owner: str) -> tuple[Path, Path, Path]:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    work = _direct_child(WORK_ROOT, run_id, stage="config.work")
    staging = _direct_child(ARTIFACT_ROOT, f".{run_id}.staging", stage="config.staging")
    final = _direct_child(ARTIFACT_ROOT, run_id, stage="config.artifact")
    require(not work.exists(), "config.work", "exists")
    require(not staging.exists(), "config.staging", "exists")
    require(not final.exists(), "config.artifact", "exists")
    work.mkdir()
    staging.mkdir()
    for directory in (work, staging):
        (directory / OWNER_FILENAME).write_text(f"{owner}\n", encoding="ascii")
    for name in ("logs", "temp", "spool", "files"):
        (work / name).mkdir()
    return work, staging, final


def _remove_owned_directory(path: Path, root: Path, owner: str, *, stage: str) -> None:
    require(path.parent.resolve() == root.resolve(), stage, "boundary")
    require(path.is_dir() and not path.is_symlink(), stage, "type")
    claim = path / OWNER_FILENAME
    require(claim.is_file(), stage, "claim_missing")
    require(claim.read_text(encoding="ascii").strip() == owner, stage, "claim")
    shutil.rmtree(path)
    require(not path.exists(), stage, "remaining")


def _migration_state(database_url: str) -> dict[str, object]:
    connection = connect_database(database_url)
    try:
        rows = connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version"
        ).fetchall()
        return {
            "count": len(rows),
            "version": str(rows[-1][0]) if rows else None,
            "integrity": str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }
    finally:
        connection.close()


def prepare_database(work: Path, runtime: RUNTIME.RuntimeSecrets) -> BrowserGraph:
    """Migrate an empty database to 0072 and seed two isolated role users."""

    database = work / "prd079-browser.sqlite3"
    database_url = _database_url(database)
    fresh = apply_migrations(database_url)
    require(fresh.applied[-1:] == [EXPECTED_MIGRATION_VERSION], "migration.fresh", "version")
    state = _migration_state(database_url)
    require(state == {
        "count": EXPECTED_MIGRATION_COUNT,
        "version": EXPECTED_MIGRATION_VERSION,
        "integrity": "ok",
        "foreign_key_violations": 0,
    }, "migration.fresh", "state")
    repeat = apply_migrations(database_url)
    require(not repeat.applied, "migration.repeat", "unexpected_apply")

    seed_values = {
        "METAHUB_SEED_ADMIN_PASSWORD": runtime.admin_password,
        "METAHUB_SEED_PROVIDER_API_KEY": runtime.provider_key,
        "METAHUB_SEED_VIRTUAL_KEY": runtime.virtual_key,
    }
    with RUNTIME.temporary_environment(seed_values):
        seed = apply_seed_data(database_url)
    now = _db_time()
    connection = connect_database(database_url)
    try:
        password_hash = hash_password(runtime.role_password)
        connection.executemany(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                (
                    "usr_prd079_browser_finance",
                    seed.tenant_id,
                    FINANCE_EMAIL,
                    "PRD-079 Finance",
                    password_hash,
                    "finance_admin",
                    now,
                    now,
                ),
                (
                    "usr_prd079_browser_developer",
                    seed.tenant_id,
                    DEVELOPER_EMAIL,
                    "PRD-079 Developer",
                    password_hash,
                    "developer",
                    now,
                    now,
                ),
            ),
        )
        connection.commit()
        finance_permissions = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT permission.code FROM roles role
                JOIN role_permissions grant_row
                  ON grant_row.tenant_id=role.tenant_id AND grant_row.role_id=role.role_id
                JOIN permissions permission
                  ON permission.tenant_id=grant_row.tenant_id
                 AND permission.permission_id=grant_row.permission_id
                WHERE role.tenant_id=? AND role.code='finance_admin'
                """,
                (seed.tenant_id,),
            ).fetchall()
        }
        require(
            {"billing.plan.read", "billing.plan.write", "billing.quota.read", "billing.quota.write"}
            <= finance_permissions,
            "database.seed",
            "finance_permissions",
        )
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
        database,
        database_url,
        seed.tenant_id,
        {"fresh": state, "repeat_applied": 0},
    )


def _source_provenance() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in SOURCE_FILES.items():
        require(
            path.is_file() and path.resolve().is_relative_to(ROOT.resolve()),
            "evidence.source",
            "missing",
        )
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _all_true(value: object, expected: set[str], *, stage: str) -> dict[str, object]:
    require(type(value) is dict and set(value) == expected, stage, "shape")
    for name, item in value.items():
        if name in {"width", "height", "overlap_count"}:
            continue
        require(item is True, stage, name)
    return value


def _validate_browser_facts(value: object) -> dict[str, object]:
    require(type(value) is dict, "browser.complete", "shape")
    require(set(value) == {
        "status", "desktop", "mobile", "lifecycle", "network", "screenshots",
        "all_108_prds_complete",
    }, "browser.complete", "shape")
    require(
        value.get("status") == "passed"
        and value.get("all_108_prds_complete") is False,
        "browser.complete",
        "status",
    )
    desktop = _all_true(value.get("desktop"), {
        "width", "height", "nonblank", "page_110", "page_111", "horizontal_overflow_free",
        "text_overflow_free", "overlap_count", "plan_authenticated", "plan_cas_exact",
        "plan_saved", "plan_native_modal", "plan_initial_focus", "plan_tab_forward_wrap",
        "plan_tab_reverse_wrap", "plan_idle_escape_closed", "plan_focus_restored",
        "plan_busy_escape_blocked", "quota_report_split",
    }, stage="browser.complete.desktop")
    require(
        desktop["width"] == 1280
        and desktop["height"] == 720
        and desktop["overlap_count"] == 0,
        "browser.complete.desktop",
        "layout",
    )
    mobile = _all_true(value.get("mobile"), {
        "width", "height", "nonblank", "page_110", "page_111", "horizontal_overflow_free",
        "text_overflow_free", "overlap_count", "quota_types_exact", "quota_adjustments_exact",
        "quota_report_split", "quota_native_modal", "quota_initial_focus",
        "quota_idle_escape_closed", "quota_reason_cleared", "quota_focus_restored",
        "quota_busy_controls_disabled", "quota_busy_escape_blocked",
    }, stage="browser.complete.mobile")
    require(
        mobile["width"] == 390
        and mobile["height"] == 844
        and mobile["overlap_count"] == 0,
        "browser.complete.mobile",
        "layout",
    )
    _all_true(value.get("lifecycle"), {
        "backend_rbac_denied", "logout_root_cleared", "logout_storage_cleared",
        "logout_late_response_discarded", "revocation_root_cleared",
        "revocation_navigation_cleared", "revocation_late_response_discarded",
    }, stage="browser.complete.lifecycle")
    network = value.get("network")
    require(type(network) is dict and set(network) == {
        "external_requests", "forbidden_queries", "console_errors", "page_errors",
        "login_statuses", "logout_statuses", "plan_patch_statuses",
        "quota_adjustment_statuses",
    }, "browser.complete.network", "shape")
    require(network == {
        "external_requests": 0,
        "forbidden_queries": 0,
        "console_errors": 0,
        "page_errors": 0,
        "login_statuses": [200, 200],
        "logout_statuses": [200],
        "plan_patch_statuses": [200],
        "quota_adjustment_statuses": [201, 201, 201, 201],
    }, "browser.complete.network", "facts")
    require(value.get("screenshots") == list(SCREENSHOTS), "browser.complete", "screenshots")
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
            image = source.convert("RGB")
            colors = image.getcolors(maxcolors=width * height)
    except (OSError, UnidentifiedImageError, ValueError):
        raise AcceptanceFailure("browser.screenshot", "decode") from None
    require(colors is not None and len(colors) >= 32, "browser.screenshot", "blank")
    dominant = max(count for count, _color in colors)
    require(dominant * 200 < width * height * 199, "browser.screenshot", "blank")


class BrowserHarness:
    def __init__(self, run_id: str, secrets_: BrowserSecrets) -> None:
        self.run_id = RUNTIME.safe_run_id(run_id)
        self.secrets = secrets_
        self.owner = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        self.work, self.staging, self.final = create_run_directories(self.run_id, self.owner)
        self.processes = RUNTIME.OwnedProcesses()
        self.graph: BrowserGraph | None = None
        self.browser_facts: dict[str, object] | None = None
        self.private_extra: tuple[str, ...] = ()
        self.backend_rbac_denied = False
        self.finance_revoked = False
        self.server: ThreadingHTTPServer | None = None
        self.control_lock = threading.Lock()

    def prepare(self) -> None:
        self._assert_ports_available()
        self.graph = prepare_database(self.work, self.secrets.runtime)
        environment = RUNTIME.service_environment(self.work, self.graph, self.secrets.runtime)
        environment["METAHUB_APPROVAL_SCHEDULER_ENABLED"] = "false"
        environment["METAHUB_APPROVAL_OUTBOX_ENABLED"] = "false"
        environment["METAHUB_APPROVAL_WRITEBACK_ENABLED"] = "false"
        self.processes.add(RUNTIME.start_resp(self.work, environment))
        gateway = self.processes.add(
            RUNTIME._BASE.start_service(
                "gateway", "metahub_gateway.app:app", GATEWAY_PORT,
                work=self.work, environment=environment,
            )
        )
        RUNTIME._BASE.wait_health(
            f"http://{LOOPBACK}:{GATEWAY_PORT}/health",
            gateway,
            stage="health.gateway",
        )
        control = self.processes.add(
            RUNTIME._BASE.start_service(
                "control", "metahub_control.app:app", RUNTIME.CONTROL_PORT,
                work=self.work, environment=environment,
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
        self.private_extra = self._verify_backend_rbac()

    def _assert_ports_available(self) -> None:
        for port in (
            GATEWAY_PORT,
            RUNTIME.CONTROL_PORT,
            RUNTIME.CONSOLE_PORT,
            RUNTIME.RESP_PORT,
            HARNESS_PORT,
        ):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind((LOOPBACK, port))
            except OSError:
                raise AcceptanceFailure("config.port", f"unavailable_{port}") from None
            finally:
                probe.close()

    def _verify_backend_rbac(self) -> tuple[str, ...]:
        """Prove a real developer Session is denied by all four backend dependencies."""

        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{RUNTIME.CONTROL_PORT}",
            timeout=httpx.Timeout(15, connect=3),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            login = client.post(
                "/auth/login",
                json={
                    "tenant_slug": "default",
                    "email": DEVELOPER_EMAIL,
                    "password": self.secrets.runtime.role_password,
                },
            )
            require(login.status_code == 200, "http.rbac.login", "status")
            try:
                token = str(login.json()["access_token"])
            except (ValueError, KeyError, TypeError):
                raise AcceptanceFailure("http.rbac.login", "shape") from None
            headers = {"Authorization": f"Bearer {token}"}
            calls = (
                client.get("/plans", headers=headers),
                client.patch("/plans", headers=headers, json={
                    "expected_revision": 0, "display_name": "Denied", "base_tier": "free",
                    "status": "active", "trial_ends_at": None,
                }),
                client.get("/quota-buckets", headers=headers),
                client.post(
                    "/quota-adjustments",
                    headers={
                        **headers,
                        "Idempotency-Key": "denied-prd079-browser",
                    },
                    json={
                        "quota_type": "paid",
                        "direction": "credit",
                        "amount": "1.000000",
                        "reason": "Denied",
                        "expected_bucket_revision": 1,
                    },
                ),
            )
            for response in calls:
                try:
                    payload = response.json()
                except ValueError:
                    raise AcceptanceFailure("http.rbac", "json") from None
                require(
                    response.status_code == 403
                    and payload.get("code") == "auth.permission_denied"
                    and bool(payload.get("request_id")),
                    "http.rbac",
                    "denial",
                )
            logout = client.post("/auth/logout", headers=headers)
            require(logout.status_code == 200, "http.rbac.logout", "status")
        self.backend_rbac_denied = True
        return (token,)

    def revoke_finance_permissions(self) -> None:
        require(self.graph is not None, "browser.revoke", "database")
        with self.control_lock:
            require(not self.finance_revoked, "browser.revoke", "already_applied")
            connection = connect_database(self.graph.database_url)
            try:
                connection.execute("BEGIN IMMEDIATE")
                role = connection.execute(
                    "SELECT role_id,permission_revision FROM roles "
                    "WHERE tenant_id=? AND code='finance_admin' AND status='active'",
                    (self.graph.tenant_id,),
                ).fetchone()
                require(role is not None, "browser.revoke", "role")
                retained = tuple(
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT permission.code FROM role_permissions grant_row
                        JOIN permissions permission
                          ON permission.tenant_id=grant_row.tenant_id
                         AND permission.permission_id=grant_row.permission_id
                        WHERE grant_row.tenant_id=? AND grant_row.role_id=?
                          AND permission.code NOT IN(
                            'billing.plan.read','billing.plan.write',
                            'billing.quota.read','billing.quota.write'
                          ) ORDER BY permission.code
                        """,
                        (self.graph.tenant_id, role["role_id"]),
                    ).fetchall()
                )
                removed = connection.execute(
                    """
                    DELETE FROM role_permissions WHERE tenant_id=? AND role_id=?
                      AND permission_id IN(
                        SELECT permission_id FROM permissions WHERE tenant_id=?
                          AND code IN(
                            'billing.plan.read','billing.plan.write',
                            'billing.quota.read','billing.quota.write'
                          )
                      )
                    """,
                    (self.graph.tenant_id, role["role_id"], self.graph.tenant_id),
                ).rowcount
                require(removed == 4, "browser.revoke", "grant_count")
                updated = connection.execute(
                    """
                    UPDATE roles SET permission_revision=permission_revision+1,
                      matrix_sha256=?,updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    WHERE tenant_id=? AND role_id=? AND permission_revision=?
                    """,
                    (
                        permission_matrix_sha256(retained), self.graph.tenant_id,
                        role["role_id"], role["permission_revision"],
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
            self.finance_revoked = True

    def serve(self) -> None:
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._reply(200, {"status": "ready", "all_108_prds_complete": False})
                else:
                    self._reply(404, {"status": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path not in {"/abort", "/complete", "/revoke-finance"}:
                    self._reply(404, {"status": "not_found"})
                    return
                supplied = self.headers.get("X-Harness-Token", "")
                if not secrets.compare_digest(supplied, harness.secrets.stop_token):
                    self._reply(403, {"status": "denied"})
                    return
                if self.path == "/abort":
                    self._reply(200, {"status": "aborting"})
                    assert harness.server is not None
                    harness.server.shutdown()
                    return
                if self.path == "/revoke-finance":
                    try:
                        harness.revoke_finance_permissions()
                    except AcceptanceFailure:
                        self._reply(409, {"status": "invalid"})
                        return
                    self._reply(200, {"status": "revoked"})
                    return
                try:
                    lengths = self.headers.get_all("Content-Length", failobj=[])
                    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                        raise ValueError("length")
                    length = int(lengths[0])
                    if not 1 <= length <= MAX_COMPLETE_BYTES:
                        raise ValueError("length")
                    harness.browser_facts = _validate_browser_facts(
                        json.loads(self.rfile.read(length))
                    )
                except (json.JSONDecodeError, ValueError, AcceptanceFailure):
                    self._reply(400, {"status": "invalid"})
                    return
                self._reply(200, {"status": "accepted"})
                assert harness.server is not None
                harness.server.shutdown()

            def _reply(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("ascii")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_arguments: object) -> None:
                return None

        self.server = ThreadingHTTPServer((LOOPBACK, HARNESS_PORT), Handler)
        self.server.daemon_threads = True
        try:
            self.server.serve_forever(poll_interval=0.2)
        finally:
            self.server.server_close()
            self.server = None

    def _database_summary(self) -> dict[str, object]:
        assert self.graph is not None
        connection = connect_database(self.graph.database_url)
        try:
            migration = _migration_state(self.graph.database_url)
            plan_count = int(connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0])
            bucket_rows = connection.execute(
                "SELECT quota_type,granted_micros FROM quota_buckets ORDER BY quota_type"
            ).fetchall()
            adjustment_types = [
                str(row[0])
                for row in connection.execute(
                    "SELECT quota_type FROM quota_adjustments ORDER BY quota_type"
                ).fetchall()
            ]
            plan_audits = int(connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='plan.created'"
            ).fetchone()[0])
            adjustment_audits = int(connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='quota.adjustment_created'"
            ).fetchone()[0])
            finance_prd079_grants = int(connection.execute(
                """
                SELECT COUNT(*) FROM roles role JOIN role_permissions grant_row
                  ON grant_row.tenant_id=role.tenant_id AND grant_row.role_id=role.role_id
                JOIN permissions permission ON permission.tenant_id=grant_row.tenant_id
                  AND permission.permission_id=grant_row.permission_id
                WHERE role.tenant_id=? AND role.code='finance_admin'
                  AND permission.code IN(
                    'billing.plan.read','billing.plan.write',
                    'billing.quota.read','billing.quota.write'
                  )
                """,
                (self.graph.tenant_id,),
            ).fetchone()[0])
            require(migration == {
                "count": 72, "version": "0072", "integrity": "ok", "foreign_key_violations": 0,
            }, "database.final", "migration")
            require(plan_count == 1, "database.final", "plan_count")
            require(
                [str(row[0]) for row in bucket_rows] == ["free", "gift", "paid", "trial"]
                and all(int(row[1]) > 0 for row in bucket_rows),
                "database.final",
                "buckets",
            )
            require(
                adjustment_types == ["free", "gift", "paid", "trial"],
                "database.final",
                "adjustments",
            )
            require(plan_audits == 1 and adjustment_audits == 4, "database.final", "audits")
            require(
                finance_prd079_grants == 0 and self.finance_revoked,
                "database.final",
                "revocation",
            )
            return {
                "migration": migration,
                "plan_count": plan_count,
                "bucket_count": len(bucket_rows),
                "adjustment_count": len(adjustment_types),
                "adjustment_types": adjustment_types,
                "plan_audit_count": plan_audits,
                "adjustment_audit_count": adjustment_audits,
                "finance_prd079_permissions_after_revocation": finance_prd079_grants,
            }
        finally:
            connection.close()

    def publish(self) -> Path:
        require(
            self.graph is not None and self.browser_facts is not None,
            "browser",
            "incomplete",
        )
        require(
            self.backend_rbac_denied and self.finance_revoked,
            "browser.lifecycle",
            "incomplete",
        )
        self.processes.close()
        RUNTIME.quiesce_database(self.graph)
        _validate_png(self.staging / SCREENSHOTS[0], 1280, 720)
        _validate_png(self.staging / SCREENSHOTS[1], 390, 844)
        _validate_png(self.staging / SCREENSHOTS[2], 1280, 720)
        _validate_png(self.staging / SCREENSHOTS[3], 390, 844)
        private_values = self.secrets.private_values(self.private_extra)
        work_privacy = RUNTIME._BASE.verify_private_storage(self.work, private_values)
        staging_privacy = RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        payload = {
            "prd_id": "PRD-079",
            "status": "passed",
            "run_id": self.run_id,
            "browser": self.browser_facts,
            "database": self._database_summary(),
            "privacy": {"work": work_privacy, "staging": staging_privacy},
            "processes": {
                "gateway": "owned_and_stopped",
                "control": "owned_and_stopped",
                "console": "owned_and_stopped",
                "resp": "owned_and_stopped",
                "harness": "owned_and_stopped",
            },
            "source_provenance": {"algorithm": "sha256", **_source_provenance()},
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
                path.name in ALLOWED_ARTIFACTS - {"sha256-manifest.json"},
                "evidence.manifest",
                "name",
            )
            require(path.is_file() and not path.is_symlink(), "evidence.manifest", "entry")
            entries.append({
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
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
        (self.staging / OWNER_FILENAME).unlink()
        os.replace(self.staging, self.final)
        require(
            {item.name for item in self.final.iterdir()} == ALLOWED_ARTIFACTS,
            "evidence.final",
            "entries",
        )
        RUNTIME._BASE.verify_private_storage(self.final, private_values)
        _remove_owned_directory(self.work, WORK_ROOT, self.owner, stage="cleanup.work")
        return self.final / "browser-acceptance.json"

    def abort(self) -> None:
        try:
            self.processes.close()
        finally:
            for path, root, stage in (
                (self.staging, ARTIFACT_ROOT, "cleanup.staging"),
                (self.work, WORK_ROOT, "cleanup.work"),
            ):
                if path.exists() and (path / OWNER_FILENAME).is_file():
                    _remove_owned_directory(path, root, self.owner, stage=stage)
            if self.server is not None:
                self.server.server_close()
                self.server = None


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(description="Serve PRD-079 browser acceptance harness")
    parser.add_argument("--run-id", required=True)
    harness: BrowserHarness | None = None
    try:
        arguments = parser.parse_args(argv)
        harness = BrowserHarness(arguments.run_id, BrowserSecrets.from_environment())
        harness.prepare()
        print(json.dumps({
            "status": "ready",
            "url": f"http://{LOOPBACK}:{RUNTIME.CONSOLE_PORT}/#plans",
            "all_108_prds_complete": False,
        }, separators=(",", ":")), flush=True)
        harness.serve()
        target = harness.publish()
        print(target.relative_to(ROOT).as_posix(), flush=True)
        return 0
    except AcceptanceFailure as exc:
        if harness is not None:
            harness.abort()
        print(
            f"PRD-079 browser harness failed: {exc.stage}:{exc.code}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    except Exception:
        if harness is not None:
            harness.abort()
        print("PRD-079 browser harness failed: harness:unexpected", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
