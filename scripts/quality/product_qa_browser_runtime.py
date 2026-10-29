"""Run isolated real-browser product journeys without touching developer data."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
for source_root in reversed(
    (
        ROOT / "shared-sdk" / "python",
        ROOT / "gateway-data-plane" / "src",
        ROOT / "control-plane-api" / "src",
    )
):
    sys.path.insert(0, str(source_root))

from metahub_shared.db import apply_migrations, apply_seed_data, connect_database  # noqa: E402
from metahub_shared.db.security import hash_password  # noqa: E402
from metahub_shared.runtime.config import load_app_config  # noqa: E402
from metahub_control.console_experience.scheduler import _refresh_workspace_dashboards  # noqa: E402

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
NODE = Path(r"C:\Program Files\nodejs\node.exe")
BROWSER_SCRIPTS = {
    "prd148-populated-domains": ROOT / "tests" / "product_qa" / "web" / "prd148PopulatedDomainsAudit.mjs",
    "workspace": ROOT / "tests" / "product_qa" / "web" / "realBrowserJourneys.mjs",
    "prd147-platform": ROOT
    / "tests"
    / "product_qa"
    / "web"
    / "prd147PlatformJourney.mjs",
    "prd148-role-audit": ROOT
    / "tests"
    / "product_qa"
    / "web"
    / "prd148RoleAudit.mjs",
    "prd148-navigation-audit": ROOT
    / "tests"
    / "product_qa"
    / "web"
    / "prd148NavigationLinkAudit.mjs",
    "prd148-public-audit": ROOT
    / "tests"
    / "product_qa"
    / "web"
    / "prd148PublicAudit.mjs",
    "prd148-partner-audit": ROOT
    / "tests"
    / "product_qa"
    / "web"
    / "prd148PartnerAudit.mjs",
    "prd148-human-interaction": ROOT
    / "tests"
    / "product_qa"
    / "web"
        / "prd148HumanInteractionAudit.mjs",
    "prd148-positive-domains": ROOT
    / "tests"
    / "product_qa"
    / "web"
    / "prd148PositiveDomainsAudit.mjs",
}
ARTIFACT_ROOT = Path(
    os.environ.get("METAHUB_QA_ARTIFACT_ROOT", ROOT / ".artifacts" / "product-qa")
).resolve()
TEMP_ROOT = ROOT / ".tmp" / "product-qa-browser"
BROWSER_JOURNEY_TIMEOUT_SECONDS = int(
    os.environ.get("METAHUB_QA_JOURNEY_TIMEOUT_SECONDS", "180")
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_http(url: str, process: subprocess.Popen[bytes]) -> None:
    # Control imports and migration-backed startup can exceed the old fixed
    # window on a cold Windows Python process. This is a service readiness
    # timeout only; browser and business assertions retain their own limits.
    startup_timeout = int(os.environ.get("METAHUB_QA_SERVICE_STARTUP_TIMEOUT_SECONDS", "90"))
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("product_qa_service_exited")
        try:
            with urlopen(url, timeout=1) as response:  # noqa: S310 - fixed loopback URL
                if response.status == 200:
                    return
        except (OSError, URLError):
            pass
        time.sleep(0.15)
    raise RuntimeError("product_qa_service_timeout")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _prepare_database(
    database_url: str, passwords: dict[str, str], *, journey_name: str
) -> dict[str, object]:
    previous_seed_password = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = passwords["admin"]
    try:
        migration = apply_migrations(database_url)
        seed = apply_seed_data(database_url)
    finally:
        if previous_seed_password is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous_seed_password

    connection = connect_database(database_url)
    try:
        journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if journal_mode != "wal":
            raise RuntimeError("product_qa_database_journal_invalid")
        # The user-authorized local automation defaults to no human challenge.
        # This changes only the disposable QA database; an explicit opt-in keeps
        # the captcha layout available for separate visual coverage.
        human_verification = os.environ.get(
            "METAHUB_QA_HUMAN_VERIFICATION_ENABLED", "false"
        ).lower() == "true"
        policy_update = connection.execute(
            "UPDATE registration_policy SET human_verification=?,revision=revision+1 "
            "WHERE environment='dev'",
            (int(human_verification),),
        )
        if policy_update.rowcount != 1:
            raise RuntimeError("product_qa_registration_policy_missing")
        for role, email, display_name in (
            ("developer", "qa-developer@example.invalid", "QA Developer"),
            ("viewer", "qa-viewer@example.invalid", "QA Viewer"),
        ):
            connection.execute(
                """
                INSERT INTO users(
                  user_id,tenant_id,email,display_name,password_hash,role,status,
                  created_at,updated_at
                ) VALUES(?,?,?,?,?,?,'active',datetime('now'),datetime('now'))
                """,
                (
                    f"usr_product_qa_{role}",
                    seed.tenant_id,
                    email,
                    display_name,
                    hash_password(passwords[role]),
                    role,
                ),
            )
        admin_user = connection.execute(
            """SELECT user.user_id,identity.account_id
                 FROM users user
                 JOIN account_identity_links identity
                   ON identity.legacy_tenant_id=user.tenant_id
                  AND identity.legacy_user_id=user.user_id
                  AND identity.status='active'
                WHERE user.tenant_id=? AND user.email='admin@metahub.local'""",
            (seed.tenant_id,),
        ).fetchone()
        if admin_user is None:
            raise RuntimeError("product_qa_admin_missing")
        admin_locale = os.environ.get("METAHUB_QA_ADMIN_LOCALE", "en-US").strip()
        if admin_locale not in {"zh-CN", "en-US"}:
            raise RuntimeError("product_qa_admin_locale_invalid")
        locale_update = connection.execute(
            "UPDATE user_preferences SET locale=? WHERE tenant_id=? AND user_id=?",
            (admin_locale, seed.tenant_id, admin_user["user_id"]),
        )
        if locale_update.rowcount == 0:
            connection.execute(
                """
                INSERT INTO user_preferences(
                  preference_id,tenant_id,user_id,active_navigation_item_id,
                  sidebar_collapsed,locale,created_at,updated_at
                ) VALUES(?,?,?,'overview',0,?,datetime('now'),datetime('now'))
                """,
                (f"upr_product_qa_{secrets.token_hex(8)}", seed.tenant_id, admin_user["user_id"], admin_locale),
            )
        account_locale_update = connection.execute(
            """
            UPDATE account_preferences
               SET locale=?,source='identity',confirmation_state='confirmed',
                   updated_at=datetime('now')
             WHERE account_id=(
               SELECT account_id FROM account_identity_links
                WHERE legacy_tenant_id=? AND legacy_user_id=? AND status='active'
             )
            """,
            (admin_locale, seed.tenant_id, admin_user["user_id"]),
        )
        if account_locale_update.rowcount != 1:
            raise RuntimeError("product_qa_admin_account_preference_missing")
        if journey_name == "prd148-positive-domains":
            binding = connection.execute(
                """SELECT workspace_id,tenant_id FROM workspace_tenant_bindings
                   WHERE tenant_id=? AND status='active' LIMIT 1""",
                (seed.tenant_id,),
            ).fetchone()
            if binding is None:
                raise RuntimeError("product_qa_partner_workspace_binding_missing")
            now = "2026-09-21T00:00:00.000Z"
            partner_id = "par_" + "1" * 32
            rule_cny = "ccr_" + "1" * 31 + "1"
            rule_usd = "ccr_" + "2" * 31 + "2"
            membership_id = "chm_" + "3" * 32
            attribution_id = "wat_" + "4" * 32
            connection.execute(
                """INSERT INTO channel_partners(
                  channel_partner_id,owner_workspace_id,tenant_id,partner_code,display_name,
                  tier_code,region_scope,contract_sha256,status,revision,created_by_operator_id,
                  updated_by_operator_id,created_at,updated_at)
                VALUES(?,?,?,?,?,'silver','cn',?,'active',1,NULL,NULL,?,?)""",
                (
                    partner_id,
                    binding["workspace_id"],
                    binding["tenant_id"],
                    "qa.browser.partner",
                    "QA Browser Partner",
                    "a" * 64,
                    now,
                    now,
                ),
            )
            for rule_id, currency in ((rule_cny, "CNY"), (rule_usd, "USD")):
                connection.execute(
                    """INSERT INTO channel_commission_rule_snapshots(
                      channel_commission_rule_snapshot_id,channel_partner_id,owner_workspace_id,
                      tenant_id,contract_sha256,tier_code,region_scope,currency,commission_bps,
                      version,effective_from,effective_until,created_at)
                    VALUES(?,?,?,?,?,'silver','cn',?,1000,1,?,NULL,?)""",
                    (
                        rule_id,
                        partner_id,
                        binding["workspace_id"],
                        binding["tenant_id"],
                        "a" * 64,
                        currency,
                        now,
                        now,
                    ),
                )
            connection.execute(
                """INSERT INTO channel_memberships(
                  channel_membership_id,channel_partner_id,owner_workspace_id,tenant_id,account_id,
                  role_code,status,revision,invited_by_operator_id,revoked_by_operator_id,
                  invitation_sha256,created_at,updated_at,revoked_at)
                VALUES(?,?,?,?,?,'partner_finance','active',1,NULL,NULL,?,?,?,NULL)""",
                (
                    membership_id,
                    partner_id,
                    binding["workspace_id"],
                    binding["tenant_id"],
                    admin_user["account_id"],
                    "b" * 64,
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO workspace_attributions(
                  workspace_attribution_id,channel_partner_id,owner_workspace_id,tenant_id,
                  target_workspace_id,target_tenant_id,channel_lead_id,
                  channel_commission_rule_snapshot_id,status,starts_at,ends_at,revision,
                  created_at,updated_at)
                VALUES(?,?,?,?,?,?,NULL,?,'active',?,NULL,1,?,?)""",
                (
                    attribution_id,
                    partner_id,
                    binding["workspace_id"],
                    binding["tenant_id"],
                    binding["workspace_id"],
                    binding["tenant_id"],
                    rule_cny,
                    now,
                    now,
                    now,
                ),
            )
        connection.commit()
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE status='applied'"
        ).fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()
    # Isolated browser QA intentionally disables background workers. Build the
    # initial read model through the real worker before opening the dashboard.
    # This is fixture preparation, not scheduler-enabled stability evidence.
    projection_prepared = _refresh_workspace_dashboards(
        load_app_config("control-plane-api").model_copy(update={"database_url": database_url})
    )
    return {
        "applied_migrations": int(migration_count),
        "latest_migration": migration.applied[-1] if migration.applied else None,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_key_violations,
        "journal_mode": journal_mode,
        "registration_human_verification_enabled": human_verification,
        "workspace_dashboard_projection_prepared_by_worker": projection_prepared,
    }


def main() -> int:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(4)}"
    run_root = TEMP_ROOT / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    browser_name = os.environ.get("METAHUB_QA_BROWSER", "chromium").lower()
    scale_name = os.environ.get("METAHUB_QA_SCALE", "1").replace(".", "_")
    journey_name = os.environ.get("METAHUB_QA_JOURNEY", "workspace").lower()
    try:
        browser_script = BROWSER_SCRIPTS[journey_name]
    except KeyError:
        raise RuntimeError("product_qa_journey_invalid") from None
    evidence_prefix = "browser-real" if journey_name == "workspace" else journey_name
    evidence_path = ARTIFACT_ROOT / (
        f"{evidence_prefix}-{browser_name}-scale-{scale_name}.json"
    )
    database_url = f"sqlite:///{(run_root / 'runtime.db').as_posix()}"
    passwords = {role: secrets.token_urlsafe(32) for role in ("admin", "developer", "viewer")}
    ephemeral_secrets = [*passwords.values()]
    database_facts = _prepare_database(database_url, passwords, journey_name=journey_name)
    if database_facts["integrity_check"] != "ok" or database_facts["foreign_key_violations"] != 0:
        raise RuntimeError("product_qa_database_invalid")

    gateway_port, control_port, console_port = _free_port(), _free_port(), _free_port()
    source_roots = (
        ROOT / "shared-sdk" / "python",
        ROOT / "gateway-data-plane" / "src",
        ROOT / "control-plane-api" / "src",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(str(path) for path in source_roots),
            "METAHUB_ENV": "dev",
            "METAHUB_DATABASE_URL": database_url,
            "METAHUB_SEED_ADMIN_PASSWORD": passwords["admin"],
            "METAHUB_AUTH_TOKEN_SECRET": secrets.token_urlsafe(48),
            "METAHUB_ALLOWED_ORIGINS": f"http://127.0.0.1:{console_port}",
            "METAHUB_CONTROL_API_BASE_URL": f"http://127.0.0.1:{control_port}",
            "METAHUB_GATEWAY_API_BASE_URL": f"http://127.0.0.1:{gateway_port}",
            "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED": "false",
            "METAHUB_PROVIDER_HEALTH_ENABLED": "false",
            "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false",
            "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false",
            "METAHUB_CONSOLE_PORT": str(console_port),
            "METAHUB_QA_CONTROL_PORT": str(control_port),
            "METAHUB_QA_GATEWAY_PORT": str(gateway_port),
            "METAHUB_QA_CONSOLE_PORT": str(console_port),
            "METAHUB_QA_ADMIN_EMAIL": "admin@metahub.local",
            "METAHUB_QA_ADMIN_PASSWORD": passwords["admin"],
            "METAHUB_QA_DEVELOPER_EMAIL": "qa-developer@example.invalid",
            "METAHUB_QA_DEVELOPER_PASSWORD": passwords["developer"],
            "METAHUB_QA_VIEWER_EMAIL": "qa-viewer@example.invalid",
            "METAHUB_QA_VIEWER_PASSWORD": passwords["viewer"],
        }
    )
    if journey_name == "prd147-platform":
        bootstrap_recovery_secret = secrets.token_urlsafe(48)
        ephemeral_secrets.append(bootstrap_recovery_secret)
        environment.update(
            {
                "METAHUB_PLATFORM_BOOTSTRAP_ENABLED": "true",
                "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": bootstrap_recovery_secret,
                "METAHUB_QA_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": bootstrap_recovery_secret,
            }
        )
    control_log = (run_root / "control.log").open("wb")
    gateway_log = (run_root / "gateway.log").open("wb")
    console_log = (run_root / "console.log").open("wb")
    control = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "metahub_control.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(control_port),
            "--no-access-log",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=control_log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    gateway = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "metahub_gateway.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(gateway_port),
            "--no-access-log",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=gateway_log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    console = subprocess.Popen(
        [str(NODE), "scripts/dev-server.mjs"],
        cwd=ROOT / "console-web",
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=console_log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        _wait_http(f"http://127.0.0.1:{control_port}/health", control)
        _wait_http(f"http://127.0.0.1:{gateway_port}/health", gateway)
        _wait_http(f"http://127.0.0.1:{console_port}/", console)
        browser_run = subprocess.run(
            [str(NODE), str(browser_script)],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=BROWSER_JOURNEY_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if browser_run.returncode != 0:
            diagnostic = browser_run.stderr.decode("utf-8", errors="replace")
            for private_value in (
                *ephemeral_secrets,
                environment["METAHUB_QA_ADMIN_EMAIL"],
                environment["METAHUB_QA_DEVELOPER_EMAIL"],
                environment["METAHUB_QA_VIEWER_EMAIL"],
            ):
                diagnostic = diagnostic.replace(private_value, "[redacted]")
            (run_root / "browser.err").write_text(diagnostic, encoding="utf-8")
            raise RuntimeError("product_qa_browser_journey_failed")
        browser_facts = json.loads(browser_run.stdout.decode("utf-8"))
        evidence = {
            "schema": "metahub.product-qa.browser.v1",
            "database": database_facts,
            "browser": browser_facts,
            "all_108_prds_complete": False,
        }
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        combined_logs = control_log.name
        del combined_logs
        journey_count = (
            len(browser_facts["journeys"]) if "journeys" in browser_facts else 1
        )
        # A browser script may complete and still report a rejected audit.
        # Propagate that verdict so the runtime cannot turn a partial/failed
        # journey into a green QA result.
        browser_passed = browser_facts.get("passed")
        if browser_passed is None:
            browser_passed = all(
                item.get("passed") is True
                for item in browser_facts.get("journeys", [])
                if isinstance(item, dict)
            ) and bool(browser_facts.get("journeys"))
        passed = browser_passed is True
        print(json.dumps({"journeys": journey_count, "passed": passed}))
        return 0 if passed else 1
    finally:
        _stop(console)
        _stop(gateway)
        _stop(control)
        console_log.close()
        gateway_log.close()
        control_log.close()
        for secret in ephemeral_secrets:
            for log_path in (
                run_root / "control.log",
                run_root / "gateway.log",
                run_root / "console.log",
            ):
                if log_path.exists() and secret.encode("utf-8") in log_path.read_bytes():
                    raise RuntimeError("product_qa_secret_in_service_log")


if __name__ == "__main__":
    raise SystemExit(main())
