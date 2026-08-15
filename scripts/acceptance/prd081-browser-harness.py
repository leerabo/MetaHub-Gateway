"""Owner-safe PAGE-113 browser harness for PRD-081.

The harness starts a real Control Plane and Console against a repository-local
SQLite database. A loopback receiver verifies the actual webhook.test bytes
without persisting bodies or signing secrets. Published evidence is restricted
to redacted facts, four screenshots, and SHA-256 manifests.
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
import threading
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[2]


def _load_runtime() -> Any:
    name = "_metahub_prd081_runtime_for_browser"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd081-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("browser harness dependency unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


RUNTIME = _load_runtime()


def _load_browser_runtime() -> Any:
    name = "_metahub_prd078_runtime_for_prd081_browser"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd078-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("browser process helper unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


BROWSER_RUNTIME = _load_browser_runtime()
AcceptanceFailure = RUNTIME.AcceptanceFailure
SafeArgumentParser = RUNTIME.SafeArgumentParser
require = RUNTIME.require
verify_webhook_signature = RUNTIME.verify_webhook_signature
connect_database = RUNTIME.connect_database

WORK_ROOT = ROOT / ".tmp" / "prd081-browser-harness"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd081-browser"
LOOPBACK = "127.0.0.1"
HARNESS_PORT = 18884
OWNER_FILENAME = ".prd081-browser-owner"
ADMIN_PASSWORD_ENV = "PRD081_BROWSER_ADMIN_PASSWORD"
ROLE_PASSWORD_ENV = "PRD081_BROWSER_ROLE_PASSWORD"
STOP_TOKEN_ENV = "PRD081_BROWSER_STOP_TOKEN"
MAX_BODY_BYTES = 64 * 1024
MAX_COMPLETE_BYTES = 24 * 1024
EXPECTED_MIGRATION_COUNT = 74
EXPECTED_MIGRATION_VERSION = "0074"
ADMIN_EMAIL = "admin@metahub.local"
VIEWER_EMAIL = "viewer-prd081@local.invalid"
SCREENSHOTS = (
    "page-113-json-desktop-1280x720.png",
    "page-113-json-delivery-desktop-1280x720.png",
    "page-113-cef-mobile-390x844.png",
    "page-113-cef-delivery-mobile-390x844.png",
)
ALLOWED_ARTIFACTS = frozenset((*SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json"))
SOURCE_FILES = {
    "browser_harness": Path(__file__).resolve(),
    "browser_acceptance": ROOT / "scripts" / "acceptance" / "prd081-browser-acceptance.mjs",
    "browser_harness_contract": ROOT
    / "scripts"
    / "acceptance"
    / "tests"
    / "test_prd081_browser_harness.py",
    "browser_driver_contract": ROOT
    / "console-web"
    / "tests"
    / "unit"
    / "prd081BrowserAcceptance.test.mjs",
    "migration_up": ROOT / "infra" / "migrations" / "0074_webhook_siem_security_event_format.sql",
    "console_main": ROOT / "console-web" / "src" / "app" / "main.js",
    "console_webhook_api": ROOT / "console-web" / "src" / "lib" / "webhooks.js",
    "console_webhook_render": ROOT
    / "console-web"
    / "src"
    / "features"
    / "webhooks"
    / "renderWebhookPanel.js",
    "console_styles": ROOT / "console-web" / "src" / "styles" / "main.css",
    "webhook_routes": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "webhooks"
    / "routes.py",
    "webhook_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "webhooks"
    / "service.py",
}

DESKTOP_FACT_KEYS = {
    "width",
    "height",
    "nonblank",
    "page_113",
    "json_mode",
    "rate_max",
    "endpoint_metadata",
    "delivery_metadata",
    "horizontal_overflow_free",
    "text_overflow_free",
    "overlap_count",
    "no_sensitive_dom",
}
MOBILE_FACT_KEYS = {
    "width",
    "height",
    "nonblank",
    "page_113",
    "cef_mode",
    "rate_min",
    "endpoint_metadata",
    "delivery_metadata",
    "horizontal_overflow_free",
    "text_overflow_free",
    "overlap_count",
    "no_sensitive_dom",
}
INTERACTION_FACT_KEYS = {
    "cef_immediate_narrowing",
    "json_no_auto_expand",
    "limit_bounds",
    "json_created",
    "cef_created",
    "json_delivery_success",
    "cef_delivery_success",
    "secrets_hidden",
    "frozen_limits_visible",
}
PERMISSION_FACT_KEYS = {
    "backend_viewer_denied",
    "viewer_navigation_hidden",
    "viewer_page_hidden",
    "logout_cleared",
}


def _stage_call(stage: str, operation: Any) -> Any:
    try:
        return operation()
    except AcceptanceFailure:
        raise
    except Exception as exc:
        foreign_stage = str(getattr(exc, "stage", ""))
        foreign_code = str(getattr(exc, "code", ""))
        safe = RUNTIME._SAFE_RUN_ID.fullmatch(foreign_stage) and RUNTIME._SAFE_RUN_ID.fullmatch(
            foreign_code
        )
        if safe:
            raise AcceptanceFailure(foreign_stage, foreign_code) from None
        raise AcceptanceFailure(stage, "failed") from None


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
            runtime=replace(
                generated,
                admin_password=admin_password,
                role_password=role_password,
            ),
            stop_token=stop_token,
        )

    def private_values(self, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        return self.runtime.values((self.stop_token, *extra))


def _direct_child(root: Path, name: str, *, stage: str) -> Path:
    require(Path(name).name == name and name not in {".", ".."}, stage, "name")
    resolved_root = root.resolve()
    target = (root / name).resolve()
    require(resolved_root.is_relative_to(ROOT.resolve()), stage, "root")
    require(target.parent == resolved_root, stage, "boundary")
    return target


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
    work.mkdir()
    staging.mkdir()
    for name in ("files", "logs", "spool", "temp"):
        (work / name).mkdir()
    for directory in (work, staging):
        (directory / OWNER_FILENAME).write_text(f"{owner}\n", encoding="ascii", newline="\n")
    return work, staging, final


def _remove_owned_directory(path: Path, root: Path, owner: str, *, stage: str) -> None:
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


def _source_provenance() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in SOURCE_FILES.items():
        require(
            path.is_file()
            and not path.is_symlink()
            and path.resolve().is_relative_to(ROOT.resolve()),
            "evidence.source",
            "missing",
        )
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _all_true(value: object, keys: set[str], *, stage: str) -> dict[str, object]:
    require(type(value) is dict and set(value) == keys, stage, "shape")
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
            "interaction",
            "permissions",
            "network",
            "screenshots",
            "all_108_prds_complete",
        },
        "browser.complete",
        "shape",
    )
    require(
        value.get("status") == "passed" and value.get("all_108_prds_complete") is False,
        "browser.complete",
        "status",
    )
    desktop = _all_true(value.get("desktop"), DESKTOP_FACT_KEYS, stage="browser.desktop")
    mobile = _all_true(value.get("mobile"), MOBILE_FACT_KEYS, stage="browser.mobile")
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
    _all_true(value.get("interaction"), INTERACTION_FACT_KEYS, stage="browser.interaction")
    _all_true(value.get("permissions"), PERMISSION_FACT_KEYS, stage="browser.permissions")
    network = value.get("network")
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
            "delivery_list_successes",
            "create_contracts_exact",
        },
        "browser.network",
        "shape",
    )
    require(
        network["external_requests"] == 0
        and network["forbidden_queries"] == 0
        and network["console_errors"] == 0
        and network["page_errors"] == 0
        and network["login_statuses"] == [200, 200]
        and network["logout_statuses"] == [200, 200]
        and network["create_statuses"] == [200, 200]
        and network["test_statuses"] == [200, 200]
        and int(network["list_successes"]) >= 3
        and int(network["delivery_list_successes"]) >= 2
        and network["create_contracts_exact"] is True,
        "browser.network",
        "values",
    )
    require(tuple(value.get("screenshots", ())) == SCREENSHOTS, "browser.screenshots", "names")
    return value


def _validate_png(path: Path, width: int, height: int) -> None:
    require(path.is_file() and not path.is_symlink(), "evidence.png", "missing")
    try:
        with Image.open(path) as image:
            image.load()
            require(
                image.format == "PNG" and image.size == (width, height), "evidence.png", "shape"
            )
            extrema = image.convert("RGB").getextrema()
            require(any(high - low >= 24 for low, high in extrema), "evidence.png", "blank")
    except (OSError, UnidentifiedImageError):
        raise AcceptanceFailure("evidence.png", "invalid") from None


def _retire_private_database_values(graph: RUNTIME.RuntimeGraph) -> None:
    with closing(connect_database(graph.database_url)) as connection:
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA secure_delete=ON")
        try:
            current = connection.execute(
                "SELECT pattern FROM dlp_rules WHERE tenant_id=? AND dlp_rule_id=?",
                (graph.tenant_id, graph.dlp_rule_id),
            ).fetchone()
            require(current is not None, "privacy.database_retire", "marker")
            original = str(current["pattern"])
            require(len(original) >= 16 and original.isascii(), "privacy.database_retire", "marker")
            retired = "retired-" + ("x" * (len(original) - len("retired-")))
            cursor = connection.execute(
                """
                UPDATE dlp_rules SET pattern=?,pattern_hash=?,updated_at=?
                WHERE tenant_id=? AND dlp_rule_id=?
                """,
                (
                    retired,
                    "sha256:" + hashlib.sha256(retired.encode("ascii")).hexdigest(),
                    RUNTIME.utc_now(),
                    graph.tenant_id,
                    graph.dlp_rule_id,
                ),
            )
            require(cursor.rowcount == 1, "privacy.database_retire", "marker")
            connection.commit()
        except AcceptanceFailure:
            raise
        except sqlite3.Error:
            raise AcceptanceFailure("privacy.database_retire", "update") from None
        try:
            require(
                int(connection.execute("PRAGMA secure_delete").fetchone()[0]) == 1
                and len(retired.encode("ascii")) == len(original.encode("ascii"))
                and connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                and connection.execute("PRAGMA foreign_key_check").fetchall() == [],
                "privacy.database_retire",
                "health",
            )
        except AcceptanceFailure:
            raise
        except sqlite3.Error:
            raise AcceptanceFailure("privacy.database_retire", "health") from None


class BrowserHarness:
    def __init__(self, run_id: str, secrets_: BrowserSecrets) -> None:
        self.run_id = RUNTIME._safe_run_id(run_id)
        self.secrets = secrets_
        self.owner = hashlib.sha256(secrets.token_bytes(48)).hexdigest()
        self.work, self.staging, self.final = create_run_directories(self.run_id, self.owner)
        self.processes = BROWSER_RUNTIME.OwnedProcesses()
        self.graph: RUNTIME.RuntimeGraph | None = None
        self.browser_facts: dict[str, object] | None = None
        self.backend_viewer_denied = False
        self.registrations: dict[str, tuple[str, str]] = {}
        self.receiver_counts: Counter[str] = Counter()
        self.receiver_invalid = 0
        self.private_extra: list[str] = []
        self.lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None

    def prepare(self) -> None:
        self._assert_ports_available()
        self.graph = RUNTIME._prepare_database(
            self.work,
            self.secrets.runtime,
            "http://127.0.0.1:1",
        )
        environment = BROWSER_RUNTIME.service_environment(
            self.work,
            self.graph,
            self.secrets.runtime,
        )
        environment.update(
            {
                "METAHUB_APPROVAL_SCHEDULER_ENABLED": "false",
                "METAHUB_APPROVAL_OUTBOX_ENABLED": "false",
                "METAHUB_APPROVAL_WRITEBACK_ENABLED": "false",
                "METAHUB_BILLING_DISPUTE_OUTBOX_ENABLED": "false",
            }
        )
        control = self.processes.add(
            BROWSER_RUNTIME._BASE.start_service(
                "control",
                "metahub_control.app:app",
                BROWSER_RUNTIME.CONTROL_PORT,
                work=self.work,
                environment=environment,
            )
        )
        BROWSER_RUNTIME._BASE.wait_health(
            f"http://{LOOPBACK}:{BROWSER_RUNTIME.CONTROL_PORT}/health",
            control,
            stage="health.control",
        )
        console = self.processes.add(
            BROWSER_RUNTIME._BASE.start_console(work=self.work, environment=environment)
        )
        BROWSER_RUNTIME._BASE.wait_health(
            f"http://{LOOPBACK}:{BROWSER_RUNTIME.CONSOLE_PORT}/",
            console,
            stage="health.console",
        )
        self._verify_backend_viewer_denied()

    def _assert_ports_available(self) -> None:
        for port in (
            BROWSER_RUNTIME.CONTROL_PORT,
            BROWSER_RUNTIME.CONSOLE_PORT,
            HARNESS_PORT,
        ):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind((LOOPBACK, port))
            except OSError:
                raise AcceptanceFailure("config.port", f"unavailable_{port}") from None
            finally:
                probe.close()

    def _verify_backend_viewer_denied(self) -> None:
        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{BROWSER_RUNTIME.CONTROL_PORT}",
            timeout=httpx.Timeout(20, connect=3),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            login = client.post(
                "/auth/login",
                json={
                    "tenant_slug": "default",
                    "email": VIEWER_EMAIL,
                    "password": self.secrets.runtime.role_password,
                },
            )
            require(login.status_code == 200, "http.viewer_login", "status")
            try:
                token = str(login.json()["access_token"])
            except (ValueError, KeyError, TypeError):
                raise AcceptanceFailure("http.viewer_login", "shape") from None
            headers = {"Authorization": f"Bearer {token}"}
            calls = (
                client.get("/webhooks", headers=headers),
                client.post(
                    "/webhooks",
                    headers=headers,
                    json={
                        "name": "denied",
                        "endpoint_url": f"http://{LOOPBACK}:{HARNESS_PORT}/receiver/denied",
                        "event_types": ["security.event_pushed"],
                        "format": "cef",
                        "security_event_rate_limit_per_minute": 1,
                    },
                ),
            )
            for response in calls:
                try:
                    payload = response.json()
                except ValueError:
                    raise AcceptanceFailure("http.viewer_rbac", "json") from None
                require(
                    response.status_code == 403
                    and payload.get("code") == "auth.permission_denied"
                    and isinstance(payload.get("request_id"), str),
                    "http.viewer_rbac",
                    "denial",
                )
            logout = client.post("/auth/logout", headers=headers)
            require(logout.status_code == 200, "http.viewer_logout", "status")
        self.private_extra.append(token)
        self.backend_viewer_denied = True

    def register_endpoint(self, value: object) -> None:
        require(
            type(value) is dict and set(value) == {"path", "signing_secret", "format"},
            "receiver.register",
            "shape",
        )
        path = str(value["path"])
        signing_secret = str(value["signing_secret"])
        payload_format = str(value["format"])
        require(path in {"/receiver/json", "/receiver/cef"}, "receiver.register", "path")
        require(
            payload_format in {"json", "cef"} and path.endswith(payload_format),
            "receiver.register",
            "format",
        )
        require(32 <= len(signing_secret) <= 512, "receiver.register", "secret")
        with self.lock:
            require(path not in self.registrations, "receiver.register", "duplicate")
            self.registrations[path] = (signing_secret, payload_format)
            self.private_extra.append(signing_secret)

    def receive(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            lengths = handler.headers.get_all("Content-Length", failobj=[])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                raise ValueError("length")
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError("length")
            body = handler.rfile.read(length)
            with self.lock:
                registration = self.registrations.get(handler.path)
            if registration is None:
                raise ValueError("path")
            signing_secret, payload_format = registration
            if not verify_webhook_signature(
                handler.headers.get("X-MetaHub-Signature", ""),
                signing_secret,
                body,
            ):
                raise ValueError("signature")
            if handler.headers.get("X-MetaHub-Event") != "webhook.test":
                raise ValueError("event")
            if payload_format == "json":
                parsed = json.loads(body.decode("utf-8", errors="strict"))
                if type(parsed) is not dict or parsed.get("event_type") != "webhook.test":
                    raise ValueError("json")
            elif not (
                body.startswith(b"CEF:0|MetaHub|AI Gateway|1|webhook.test|Webhook test|")
                and b"\r" not in body
                and b"\n" not in body
            ):
                raise ValueError("cef")
            with self.lock:
                self.receiver_counts[payload_format] += 1
        except (UnicodeError, ValueError, json.JSONDecodeError):
            with self.lock:
                self.receiver_invalid += 1
            handler.send_response(400)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        handler.send_response(204)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def serve(self) -> None:
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._reply(200, {"status": "ready", "all_108_prds_complete": False})
                else:
                    self._reply(404, {"status": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path.startswith("/receiver/"):
                    harness.receive(self)
                    return
                if self.path not in {"/abort", "/complete", "/register-endpoint"}:
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
                try:
                    payload = self._read_json(
                        MAX_COMPLETE_BYTES if self.path == "/complete" else MAX_BODY_BYTES
                    )
                    if self.path == "/register-endpoint":
                        harness.register_endpoint(payload)
                        self._reply(200, {"status": "registered"})
                        return
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
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_arguments: object) -> None:
                return

        self.server = ThreadingHTTPServer((LOOPBACK, HARNESS_PORT), Handler)
        self.server.daemon_threads = True
        try:
            self.server.serve_forever(poll_interval=0.05)
        finally:
            self.server.server_close()
            self.server = None

    def _database_summary(self) -> dict[str, object]:
        require(self.graph is not None, "database.final", "missing")
        with closing(connect_database(self.graph.database_url)) as connection:
            migration = RUNTIME._migration_health(
                self.graph.database_url,
                EXPECTED_MIGRATION_COUNT,
                EXPECTED_MIGRATION_VERSION,
                "database.final",
            )
            endpoints = connection.execute(
                """
                SELECT payload_format,security_event_rate_limit_per_minute,event_types_json
                FROM webhook_endpoints WHERE tenant_id=? ORDER BY payload_format
                """,
                (self.graph.tenant_id,),
            ).fetchall()
            deliveries = connection.execute(
                """
                SELECT payload_format,security_event_rate_limit_per_minute,status,
                       attempt_count,event_type
                FROM webhook_deliveries WHERE tenant_id=? ORDER BY payload_format
                """,
                (self.graph.tenant_id,),
            ).fetchall()
            endpoint_contracts = {
                (
                    str(row["payload_format"]),
                    int(row["security_event_rate_limit_per_minute"]),
                    str(row["event_types_json"]),
                )
                for row in endpoints
            }
            delivery_contracts = {
                (
                    str(row["payload_format"]),
                    int(row["security_event_rate_limit_per_minute"]),
                    str(row["status"]),
                    int(row["attempt_count"]),
                    str(row["event_type"]),
                )
                for row in deliveries
            }
            require(
                endpoint_contracts
                == {
                    ("json", 600, '["security.event_pushed"]'),
                    ("cef", 1, '["security.event_pushed"]'),
                },
                "database.final",
                "endpoints",
            )
            require(
                delivery_contracts
                == {
                    ("json", 600, "success", 1, "webhook.test"),
                    ("cef", 1, "success", 1, "webhook.test"),
                },
                "database.final",
                "deliveries",
            )
            require(
                connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                and connection.execute("PRAGMA foreign_key_check").fetchall() == [],
                "database.final",
                "health",
            )
        with self.lock:
            receiver_counts = dict(self.receiver_counts)
            receiver_invalid = self.receiver_invalid
        require(
            receiver_counts == {"json": 1, "cef": 1} and receiver_invalid == 0,
            "receiver.final",
            "counts",
        )
        return {
            "migration": migration,
            "endpoint_count": len(endpoints),
            "delivery_count": len(deliveries),
            "json_endpoint_rate_limit": 600,
            "cef_endpoint_rate_limit": 1,
            "successful_delivery_count": 2,
            "receiver_json_count": receiver_counts["json"],
            "receiver_cef_count": receiver_counts["cef"],
            "receiver_invalid_count": receiver_invalid,
        }

    def publish(self) -> Path:
        require(
            self.graph is not None
            and self.browser_facts is not None
            and self.backend_viewer_denied,
            "browser",
            "incomplete",
        )
        with self.lock:
            require(
                set(self.registrations) == {"/receiver/json", "/receiver/cef"},
                "receiver.final",
                "registrations",
            )
        _stage_call("cleanup.processes", self.processes.close)
        _stage_call(
            "cleanup.quiesce_before_privacy",
            lambda: RUNTIME._quiesce_database(self.graph),
        )
        _stage_call(
            "privacy.database_retire",
            lambda: _retire_private_database_values(self.graph),
        )
        _stage_call(
            "cleanup.quiesce_after_privacy",
            lambda: RUNTIME._quiesce_database(self.graph),
        )
        for name, dimensions in zip(
            SCREENSHOTS,
            ((1280, 720), (1280, 720), (390, 844), (390, 844)),
            strict=True,
        ):
            _stage_call(
                "evidence.png",
                lambda name=name, dimensions=dimensions: _validate_png(
                    self.staging / name, *dimensions
                ),
            )
        private_values = self.secrets.private_values(tuple(self.private_extra))
        work_privacy = _stage_call(
            "privacy.work",
            lambda: BROWSER_RUNTIME._BASE.verify_private_storage(self.work, private_values),
        )
        staging_privacy = _stage_call(
            "privacy.staging",
            lambda: BROWSER_RUNTIME._BASE.verify_private_storage(self.staging, private_values),
        )
        payload = {
            "prd_id": "PRD-081",
            "status": "passed",
            "run_id": self.run_id,
            "browser": self.browser_facts,
            "database": _stage_call("database.final", self._database_summary),
            "privacy": {
                "work": work_privacy,
                "staging": staging_privacy,
            },
            "processes": {
                "control": "owned_and_stopped",
                "console": "owned_and_stopped",
                "receiver": "owned_and_stopped",
            },
            "source_provenance": {
                "algorithm": "sha256",
                **_stage_call("evidence.source", _source_provenance),
            },
            "all_108_prds_complete": False,
        }
        _stage_call(
            "evidence.privacy",
            lambda: RUNTIME.assert_safe_evidence(payload, private_values),
        )
        evidence = self.staging / "browser-acceptance.json"
        _stage_call(
            "evidence.write",
            lambda: evidence.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            ),
        )
        _stage_call(
            "privacy.evidence",
            lambda: BROWSER_RUNTIME._BASE.verify_private_storage(self.staging, private_values),
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
        manifest = {
            "algorithm": "sha256",
            "files": entries,
            "all_108_prds_complete": False,
        }
        _stage_call(
            "evidence.manifest",
            lambda: (self.staging / "sha256-manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            ),
        )
        require(
            {item.name for item in self.staging.iterdir()} == ALLOWED_ARTIFACTS | {OWNER_FILENAME},
            "evidence.whitelist",
            "entries",
        )
        _stage_call(
            "privacy.manifest",
            lambda: BROWSER_RUNTIME._BASE.verify_private_storage(self.staging, private_values),
        )

        def promote() -> None:
            (self.staging / OWNER_FILENAME).unlink()
            os.replace(self.staging, self.final)
            require(
                {item.name for item in self.final.iterdir()} == ALLOWED_ARTIFACTS,
                "evidence.final",
                "entries",
            )

        _stage_call("evidence.publish", promote)
        _stage_call(
            "privacy.final",
            lambda: BROWSER_RUNTIME._BASE.verify_private_storage(self.final, private_values),
        )
        _stage_call(
            "cleanup.work",
            lambda: _remove_owned_directory(self.work, WORK_ROOT, self.owner, stage="cleanup.work"),
        )
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
    parser = SafeArgumentParser(description="Serve PRD-081 PAGE-113 browser harness")
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
                    "url": f"http://{LOOPBACK}:{BROWSER_RUNTIME.CONSOLE_PORT}/#webhooks",
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
            try:
                harness.abort()
            except Exception:
                pass
        print(
            f"PRD-081 browser harness failed: {exc.stage}:{exc.code}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    except Exception:
        if harness is not None:
            try:
                harness.abort()
            except Exception:
                pass
        print("PRD-081 browser harness failed: harness:unexpected", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
