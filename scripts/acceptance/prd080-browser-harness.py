"""Owner-safe loopback harness for PRD-080 PAGE-112 browser acceptance.

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
WORK_ROOT = ROOT / ".tmp" / "prd080-browser-harness"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd080-browser"
LOOPBACK = "127.0.0.1"
HARNESS_PORT = 18883
OWNER_FILENAME = ".prd080-browser-owner"
ROLE_PASSWORD_ENV = "PRD080_BROWSER_ROLE_PASSWORD"
STOP_TOKEN_ENV = "PRD080_BROWSER_STOP_TOKEN"
REQUEST_REASON_ENV = "PRD080_BROWSER_REQUEST_REASON"
TIME_REASON_ENV = "PRD080_BROWSER_TIME_REASON"
APPROVAL_REASON_ENV = "PRD080_BROWSER_APPROVAL_REASON"
REJECTION_REASON_ENV = "PRD080_BROWSER_REJECTION_REASON"
MAX_COMPLETE_BYTES = 16 * 1024
MAX_PRIVATE_REGISTRATION_BYTES = 8 * 1024
EXPECTED_MIGRATION_COUNT = 73
EXPECTED_MIGRATION_VERSION = "0073"
APPLICANT_EMAIL = "applicant@prd080.runtime"
REVIEWER_EMAIL = "reviewer@prd080.runtime"
DEVELOPER_EMAIL = "developer@prd080.runtime"
APPLICANT_USER_ID = "usr_prd080_browser_applicant"
REVIEWER_USER_ID = "usr_prd080_browser_reviewer"
REQUEST_SCOPE_IDS = (
    "req_prd080_browser_request_a",
    "req_prd080_browser_request_b",
)
TIME_SCOPE_IDS = (
    "req_prd080_browser_time_a",
    "req_prd080_browser_time_b",
)
REQUEST_SCOPE_TIMES = (
    "2026-08-12T12:00:00Z",
    "2026-08-12T12:05:00Z",
)
TIME_SCOPE_TIMES = (
    "2026-08-13T12:00:00Z",
    "2026-08-13T12:05:00Z",
)
SCREENSHOTS = (
    "page-112-list-desktop-1280x720.png",
    "page-112-list-mobile-390x844.png",
    "page-112-approved-desktop-1280x720.png",
    "page-112-rejected-mobile-390x844.png",
)
ALLOWED_ARTIFACTS = frozenset(
    (*SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json")
)
SOURCE_FILES = {
    "browser_harness": Path(__file__).resolve(),
    "browser_acceptance": ROOT / "scripts" / "acceptance" / "prd080-browser-acceptance.mjs",
    "migration_up": ROOT / "infra" / "migrations" / "0073_billing_dispute_adjustment_workflow.sql",
    "console_main": ROOT / "console-web" / "src" / "app" / "main.js",
    "billing_dispute_api": ROOT / "console-web" / "src" / "lib" / "billingDisputes.js",
    "billing_dispute_state": ROOT
    / "console-web"
    / "src"
    / "features"
    / "billing-disputes"
    / "billingDisputePageState.js",
    "billing_dispute_binding": ROOT
    / "console-web"
    / "src"
    / "features"
    / "billing-disputes"
    / "bindBillingDisputes.js",
    "billing_dispute_render": ROOT
    / "console-web"
    / "src"
    / "features"
    / "billing-disputes"
    / "renderBillingDisputes.js",
    "billing_dispute_routes": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "billing_disputes"
    / "routes.py",
    "billing_dispute_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "billing_disputes"
    / "service.py",
    "billing_dispute_styles": ROOT / "console-web" / "src" / "styles" / "main.css",
}


def _load_runtime_module() -> Any:
    """Load only the proven process/config helpers from PRD-078."""

    name = "_metahub_prd078_runtime_helpers_for_prd080_browser"
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
    request_reason: str = field(repr=False)
    time_reason: str = field(repr=False)
    approval_reason: str = field(repr=False)
    rejection_reason: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> BrowserSecrets:
        role_password = os.environ.get(ROLE_PASSWORD_ENV, "")
        stop_token = os.environ.get(STOP_TOKEN_ENV, "")
        private_text = tuple(
            os.environ.get(name, "")
            for name in (
                REQUEST_REASON_ENV,
                TIME_REASON_ENV,
                APPROVAL_REASON_ENV,
                REJECTION_REASON_ENV,
            )
        )
        require(len(role_password) >= 32, "config.role_password", "invalid")
        require(len(stop_token) >= 43, "config.stop_token", "invalid")
        require(
            all(32 <= len(value.encode("utf-8")) <= 1000 for value in private_text)
            and len(set(private_text)) == len(private_text),
            "config.private_text",
            "invalid",
        )
        generated = RUNTIME.RuntimeSecrets.create()
        return cls(
            runtime=replace(generated, role_password=role_password),
            stop_token=stop_token,
            request_reason=private_text[0],
            time_reason=private_text[1],
            approval_reason=private_text[2],
            rejection_reason=private_text[3],
        )

    def credential_values(self, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        return self.runtime.values((self.stop_token, *extra))

    def private_values(self, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        return self.credential_values(
            (
                self.request_reason,
                self.time_reason,
                self.approval_reason,
                self.rejection_reason,
                *extra,
            )
        )


@dataclass(frozen=True, slots=True)
class BrowserGraph:
    database: Path = field(repr=False)
    database_url: str = field(repr=False)
    tenant_id: str = field(repr=False)
    applicant_user_id: str = field(repr=False)
    reviewer_user_id: str = field(repr=False)
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


def _insert_charge(
    connection: Any,
    owner: Any,
    request_id: str,
    suffix: str,
    created_at: str,
    amount: str,
) -> str:
    """Insert real Request/Price/Usage facts and return the trigger-created charge ID."""

    trace_id = f"trc_prd080_browser_{suffix}"
    snapshot_id = f"prs_prd080_browser_{suffix}"
    usage_id = f"usg_prd080_browser_{suffix}"
    schema_version = "prd080-browser-price-v1"
    normalized = json.dumps(
        {
            "cache_tokens": 0,
            "cost_amount": amount,
            "cost_cny": amount,
            "cost_usd": "0",
            "currency": "CNY",
            "input_tokens": 11,
            "output_tokens": 7,
            "posting_source": "direct",
            "price_schema_version": schema_version,
            "price_snapshot_id": snapshot_id,
            "total_tokens": 18,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,
          snapshot_version,policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,?,NULL,'disabled',?)
        """,
        (owner["tenant_id"], request_id, trace_id, owner["virtual_key_id"], created_at),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,
          endpoint,request_model,resolved_model,provider_code,status,
          input_tokens,output_tokens,total_tokens,estimated_cost_amount,
          currency,price_schema_version,environment_id,environment_code,
          project_id,application_id,latency_ms,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'chat/completions',?,?,?,'success',11,7,18,
                 ?,'CNY',?,?,?,?,?,17,?,?)
        """,
        (
            request_id,
            owner["tenant_id"],
            trace_id,
            owner["virtual_key_id"],
            owner["provider_id"],
            owner["model_id"],
            owner["model_alias"],
            owner["model_alias"],
            owner["provider_code"],
            amount,
            schema_version,
            owner["environment_id"],
            owner["environment_code"],
            owner["project_id"],
            owner["application_id"],
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO price_snapshots(
          price_snapshot_id,tenant_id,request_id,model_id,provider_id,provider_code,
          currency,input_price_per_1k,output_price_per_1k,price_schema_version,
          billing_dimensions,provider_raw_mapping,source,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'CNY','0.001000','0.002000',?,'{}','{}',
                 'model_catalog','active',?,?)
        """,
        (
            snapshot_id,
            owner["tenant_id"],
            request_id,
            owner["model_id"],
            owner["provider_id"],
            owner["provider_code"],
            schema_version,
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,
          total_tokens,cache_tokens,raw_usage,normalized_usage,cost_amount,
          cost_cny,cost_usd,currency,price_snapshot_id,price_schema_version,
          model_id,provider_id,virtual_key_id,endpoint,request_model,resolved_model,
          provider_code,environment_id,environment_code,project_id,application_id,
          estimated,usage_source,created_at,updated_at
        ) VALUES(?,?,?,11,7,18,0,'{}',?,?,?,0,'CNY',?,?,?,?,?,
                 'chat/completions',?,?,?,?,?,?,?,0,'upstream',?,?)
        """,
        (
            usage_id,
            owner["tenant_id"],
            request_id,
            normalized,
            amount,
            amount,
            snapshot_id,
            schema_version,
            owner["model_id"],
            owner["provider_id"],
            owner["virtual_key_id"],
            owner["model_alias"],
            owner["model_alias"],
            owner["provider_code"],
            owner["environment_id"],
            owner["environment_code"],
            owner["project_id"],
            owner["application_id"],
            created_at,
            created_at,
        ),
    )
    charge = connection.execute(
        """
        SELECT finops_ledger_entry_id,direction,amount_micros,currency,status
        FROM finops_ledger_entries
        WHERE tenant_id=? AND usage_ledger_id=? AND entry_type='usage_charge'
        """,
        (owner["tenant_id"], usage_id),
    ).fetchone()
    expected_micros = int(amount.replace(".", ""))
    require(
        charge is not None
        and tuple(charge)[1:] == ("debit", expected_micros, "CNY", "posted"),
        "database.seed",
        "finops_projection",
    )
    return str(charge["finops_ledger_entry_id"])


def prepare_database(work: Path, runtime: RUNTIME.RuntimeSecrets) -> BrowserGraph:
    """Migrate an empty database to 0073 and seed two disjoint charge scopes."""

    database = work / "prd080-browser.sqlite3"
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
                    APPLICANT_USER_ID,
                    seed.tenant_id,
                    APPLICANT_EMAIL,
                    "PRD-080 Applicant",
                    password_hash,
                    "finance_admin",
                    now,
                    now,
                ),
                (
                    REVIEWER_USER_ID,
                    seed.tenant_id,
                    REVIEWER_EMAIL,
                    "PRD-080 Reviewer",
                    password_hash,
                    "finance_admin",
                    now,
                    now,
                ),
                (
                    "usr_prd080_browser_developer",
                    seed.tenant_id,
                    DEVELOPER_EMAIL,
                    "PRD-080 Developer",
                    password_hash,
                    "developer",
                    now,
                    now,
                ),
            ),
        )
        owner = connection.execute(
            """
            SELECT tenant.tenant_id,provider.provider_id,provider.provider_code,
                   model.model_id,model.model_alias,key.virtual_key_id,
                   key.environment_id,environment.code AS environment_code,
                   key.project_id,key.application_id
            FROM tenants tenant
            JOIN providers provider ON provider.tenant_id=tenant.tenant_id
            JOIN models model
              ON model.tenant_id=provider.tenant_id AND model.provider_id=provider.provider_id
            JOIN virtual_keys key ON key.tenant_id=tenant.tenant_id
            JOIN environments environment
              ON environment.tenant_id=key.tenant_id
             AND environment.environment_id=key.environment_id
            WHERE tenant.tenant_id=?
            ORDER BY provider.provider_id,model.model_id,key.virtual_key_id LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()
        require(owner is not None, "database.seed", "owner")
        charges = []
        for index, (request_id, created_at, amount) in enumerate(
            zip(
                (*REQUEST_SCOPE_IDS, *TIME_SCOPE_IDS),
                (*REQUEST_SCOPE_TIMES, *TIME_SCOPE_TIMES),
                ("2.000000", "3.000000", "4.000000", "5.000000"),
                strict=True,
            )
        ):
            charges.append(
                _insert_charge(
                    connection,
                    owner,
                    request_id,
                    f"charge_{index + 1}",
                    created_at,
                    amount,
                )
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
            {
                "billing.dispute.read",
                "billing.dispute.create",
                "billing.dispute.review",
                "billing.finops.write",
            }
            <= finance_permissions,
            "database.seed",
            "finance_permissions",
        )
        require(len(set(charges)) == 4, "database.seed", "charge_count")
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
        APPLICANT_USER_ID,
        REVIEWER_USER_ID,
        {"fresh": state, "repeat_applied": 0, "seeded_charge_count": 4},
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
        "width", "height", "nonblank", "page_112", "horizontal_overflow_free",
        "text_overflow_free", "overlap_count", "request_scope_created",
        "time_scope_created", "list_rows_exact", "applicant_self_review_blocked",
        "request_detail_exact", "approve_terminal", "partial_adjustment",
        "approved_timeline_exact", "private_text_collapsed", "html_escape_safe",
    }, stage="browser.complete.desktop")
    require(
        desktop["width"] == 1280
        and desktop["height"] == 720
        and desktop["overlap_count"] == 0,
        "browser.complete.desktop",
        "layout",
    )
    mobile = _all_true(value.get("mobile"), {
        "width", "height", "nonblank", "page_112", "horizontal_overflow_free",
        "text_overflow_free", "overlap_count", "list_rows_exact", "reject_terminal",
        "rejected_timeline_exact", "native_dialog", "initial_focus",
        "tab_forward_wrap", "tab_reverse_wrap", "idle_escape_closed",
        "focus_restored", "busy_controls_disabled", "busy_escape_blocked",
        "reason_cleared", "decision_cleared", "private_text_collapsed",
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
        "registered_private_values",
    }, stage="browser.complete.lifecycle")
    network = value.get("network")
    require(type(network) is dict and set(network) == {
        "external_requests", "forbidden_queries", "console_errors", "page_errors",
        "login_statuses", "logout_statuses", "create_statuses",
        "transition_statuses", "list_successes", "detail_successes",
    }, "browser.complete.network", "shape")
    require(
        network.get("external_requests") == 0
        and network.get("forbidden_queries") == 0
        and network.get("console_errors") == 0
        and network.get("page_errors") == 0
        and network.get("login_statuses") == [200, 200]
        and network.get("logout_statuses") == [200]
        and network.get("create_statuses") == [201, 201]
        and network.get("transition_statuses") == [200, 200, 200, 200]
        and type(network.get("list_successes")) is int
        and network["list_successes"] >= 2
        and type(network.get("detail_successes")) is int
        and network["detail_successes"] >= 2,
        "browser.complete.network",
        "facts",
    )
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


def _verify_private_outside_database(
    root: Path,
    database: Path,
    private_values: tuple[str, ...],
) -> dict[str, int]:
    """Scan logs/spool/temp/artifacts while allowing private reason text only in SQLite."""

    require(root.resolve().is_relative_to(ROOT.resolve()), "privacy.work", "boundary")
    database_resolved = database.resolve()
    encoded = tuple(value.encode("utf-8") for value in private_values if value)
    scanned = 0
    matches = 0
    for path in root.rglob("*"):
        require(not path.is_symlink(), "privacy.work", "symlink")
        if not path.is_file() or path.resolve() == database_resolved:
            continue
        scanned += 1
        data = path.read_bytes()
        matches += sum(data.count(value) for value in encoded)
    require(matches == 0, "privacy.work", "private_value")
    return {"files_scanned": scanned, "plaintext_matches": matches}


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
        self.registered_private_values = False
        self.server: ThreadingHTTPServer | None = None
        self.control_lock = threading.Lock()

    def prepare(self) -> None:
        self._assert_ports_available()
        self.graph = prepare_database(self.work, self.secrets.runtime)
        environment = RUNTIME.service_environment(self.work, self.graph, self.secrets.runtime)
        environment["METAHUB_APPROVAL_SCHEDULER_ENABLED"] = "false"
        environment["METAHUB_APPROVAL_OUTBOX_ENABLED"] = "false"
        environment["METAHUB_APPROVAL_WRITEBACK_ENABLED"] = "false"
        environment["METAHUB_BILLING_DISPUTE_OUTBOX_ENABLED"] = "false"
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
            RUNTIME.CONTROL_PORT,
            RUNTIME.CONSOLE_PORT,
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
        """Prove a real developer Session is denied by every dispute dependency."""

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
                client.get("/billing-disputes", headers=headers),
                client.get(
                    "/billing-disputes/bdsp_00000000000000000000000000000000",
                    headers=headers,
                ),
                client.post(
                    "/billing-disputes",
                    headers={
                        **headers,
                        "Idempotency-Key": "denied-prd080-create",
                    },
                    json={
                        "scope_type": "request_ids",
                        "request_ids": [REQUEST_SCOPE_IDS[0]],
                        "reason": "Denied",
                    },
                ),
                client.patch(
                    "/billing-disputes/bdsp_00000000000000000000000000000000",
                    headers={
                        **headers,
                        "Idempotency-Key": "denied-prd080-review",
                    },
                    json={"action": "start_review", "expected_revision": 1},
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
                            'billing.dispute.read','billing.dispute.create',
                            'billing.dispute.review'
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
                            'billing.dispute.read','billing.dispute.create',
                            'billing.dispute.review'
                          )
                      )
                    """,
                    (self.graph.tenant_id, role["role_id"], self.graph.tenant_id),
                ).rowcount
                require(removed == 3, "browser.revoke", "grant_count")
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
                if self.path not in {
                    "/abort",
                    "/complete",
                    "/register-private",
                    "/revoke-finance",
                }:
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
                if self.path == "/register-private":
                    try:
                        lengths = self.headers.get_all("Content-Length", failobj=[])
                        if (
                            len(lengths) != 1
                            or not lengths[0].isascii()
                            or not lengths[0].isdecimal()
                        ):
                            raise ValueError("length")
                        length = int(lengths[0])
                        if not 1 <= length <= MAX_PRIVATE_REGISTRATION_BYTES:
                            raise ValueError("length")
                        payload = json.loads(self.rfile.read(length))
                        if type(payload) is not dict or set(payload) != {"values"}:
                            raise ValueError("shape")
                        values = payload["values"]
                        if (
                            type(values) is not list
                            or not 1 <= len(values) <= 16
                            or any(
                                type(value) is not str
                                or not 8 <= len(value) <= 128
                                or any(not 33 <= ord(char) <= 126 for char in value)
                                for value in values
                            )
                            or len(set(values)) != len(values)
                            or any(value in harness.private_extra for value in values)
                        ):
                            raise ValueError("values")
                    except (json.JSONDecodeError, ValueError):
                        self._reply(400, {"status": "invalid"})
                        return
                    harness.private_extra = (*harness.private_extra, *tuple(values))
                    harness.registered_private_values = True
                    self._reply(200, {"status": "accepted"})
                    return
                if not harness.registered_private_values:
                    self._reply(409, {"status": "private_values_missing"})
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
            statuses = {
                str(row["status"]): int(row["item_count"])
                for row in connection.execute(
                    "SELECT status,COUNT(*) AS item_count FROM billing_disputes GROUP BY status"
                ).fetchall()
            }
            scopes = {
                str(row["scope_type"]): int(row["item_count"])
                for row in connection.execute(
                    "SELECT scope_type,COUNT(*) AS item_count "
                    "FROM billing_disputes GROUP BY scope_type"
                ).fetchall()
            }
            item_count = int(
                connection.execute("SELECT COUNT(*) FROM billing_dispute_items").fetchone()[0]
            )
            hold_rows = connection.execute(
                "SELECT status,COUNT(*) AS item_count FROM billing_settlement_holds GROUP BY status"
            ).fetchall()
            holds = {str(row["status"]): int(row["item_count"]) for row in hold_rows}
            transitions = {
                str(row["action"]): int(row["item_count"])
                for row in connection.execute(
                    "SELECT action,COUNT(*) AS item_count "
                    "FROM billing_dispute_transitions GROUP BY action"
                ).fetchall()
            }
            link_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM billing_dispute_adjustment_links"
                ).fetchone()[0]
            )
            adjustment_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM finops_adjustments adjustment
                    JOIN billing_dispute_adjustment_links link
                      ON link.tenant_id=adjustment.tenant_id
                     AND link.finops_adjustment_id=adjustment.finops_adjustment_id
                    WHERE adjustment.adjustment_type='reversal'
                      AND adjustment.status='posted'
                    """
                ).fetchone()[0]
            )
            reversal_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM finops_ledger_entries reversal
                    JOIN billing_dispute_adjustment_links link
                      ON link.tenant_id=reversal.tenant_id
                     AND link.reversal_finops_ledger_entry_id=reversal.finops_ledger_entry_id
                    WHERE reversal.entry_type='reversal' AND reversal.direction='credit'
                      AND reversal.status='posted'
                    """
                ).fetchone()[0]
            )
            event_count = int(
                connection.execute("SELECT COUNT(*) FROM billing_dispute_events").fetchone()[0]
            )
            outbox_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM billing_dispute_event_outbox"
                ).fetchone()[0]
            )
            audit_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE action LIKE 'billing_dispute.%'"
                ).fetchone()[0]
            )
            private_text_rows = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM billing_disputes
                    WHERE reason IN(?,?) AND decision_reason IN(?,?)
                    """,
                    (
                        self.secrets.request_reason,
                        self.secrets.time_reason,
                        self.secrets.approval_reason,
                        self.secrets.rejection_reason,
                    ),
                ).fetchone()[0]
            )
            finance_prd080_grants = int(connection.execute(
                """
                SELECT COUNT(*) FROM roles role JOIN role_permissions grant_row
                  ON grant_row.tenant_id=role.tenant_id AND grant_row.role_id=role.role_id
                JOIN permissions permission ON permission.tenant_id=grant_row.tenant_id
                  AND permission.permission_id=grant_row.permission_id
                WHERE role.tenant_id=? AND role.code='finance_admin'
                  AND permission.code IN(
                    'billing.dispute.read','billing.dispute.create',
                    'billing.dispute.review'
                  )
                """,
                (self.graph.tenant_id,),
            ).fetchone()[0])
            require(migration == {
                "count": 73, "version": "0073", "integrity": "ok", "foreign_key_violations": 0,
            }, "database.final", "migration")
            require(statuses == {"approved": 1, "rejected": 1}, "database.final", "statuses")
            require(scopes == {"request_ids": 1, "time_range": 1}, "database.final", "scopes")
            require(item_count == 4, "database.final", "items")
            require(holds == {"released": 4}, "database.final", "holds")
            require(
                transitions == {"approve": 1, "create": 2, "reject": 1, "start_review": 2},
                "database.final",
                "transitions",
            )
            require(
                link_count == adjustment_count == reversal_count == 1,
                "database.final",
                "adjustments",
            )
            require(
                event_count == outbox_count == 2 and audit_count == 6,
                "database.final",
                "events_audits",
            )
            require(private_text_rows == 2, "database.final", "private_text")
            require(
                finance_prd080_grants == 0 and self.finance_revoked,
                "database.final",
                "revocation",
            )
            return {
                "migration": migration,
                "dispute_count": sum(statuses.values()),
                "status_counts": statuses,
                "scope_counts": scopes,
                "item_count": item_count,
                "released_hold_count": holds.get("released", 0),
                "transition_count": sum(transitions.values()),
                "adjustment_link_count": link_count,
                "finops_reversal_count": reversal_count,
                "event_count": event_count,
                "audit_count": audit_count,
                "finance_prd080_permissions_after_revocation": finance_prd080_grants,
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
            self.backend_rbac_denied and self.finance_revoked and self.registered_private_values,
            "browser.lifecycle",
            "incomplete",
        )
        self.processes.close()
        RUNTIME.quiesce_database(self.graph)
        _validate_png(self.staging / SCREENSHOTS[0], 1280, 720)
        _validate_png(self.staging / SCREENSHOTS[1], 390, 844)
        _validate_png(self.staging / SCREENSHOTS[2], 1280, 720)
        _validate_png(self.staging / SCREENSHOTS[3], 390, 844)
        credential_values = self.secrets.credential_values(self.private_extra)
        private_values = self.secrets.private_values(self.private_extra)
        work_credentials = RUNTIME._BASE.verify_private_storage(self.work, credential_values)
        work_non_database = _verify_private_outside_database(
            self.work,
            self.graph.database,
            private_values,
        )
        staging_privacy = RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        payload = {
            "prd_id": "PRD-080",
            "status": "passed",
            "run_id": self.run_id,
            "browser": self.browser_facts,
            "database": self._database_summary(),
            "privacy": {
                "work_credentials": work_credentials,
                "work_non_database": work_non_database,
                "staging": staging_privacy,
            },
            "processes": {
                "control": "owned_and_stopped",
                "console": "owned_and_stopped",
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
    parser = SafeArgumentParser(description="Serve PRD-080 browser acceptance harness")
    parser.add_argument("--run-id", required=True)
    harness: BrowserHarness | None = None
    try:
        arguments = parser.parse_args(argv)
        harness = BrowserHarness(arguments.run_id, BrowserSecrets.from_environment())
        harness.prepare()
        print(json.dumps({
            "status": "ready",
            "url": f"http://{LOOPBACK}:{RUNTIME.CONSOLE_PORT}/#billing-disputes",
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
            f"PRD-080 browser harness failed: {exc.stage}:{exc.code}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    except Exception:
        if harness is not None:
            harness.abort()
        print("PRD-080 browser harness failed: harness:unexpected", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
