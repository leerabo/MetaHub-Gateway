"""PRD-080 Browser harness ownership, database, privacy and publication gates."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "scripts" / "acceptance" / "prd080-browser-harness.py"


def _load_harness():
    name = "_metahub_prd080_browser_harness_tests"
    current = sys.modules.get(name)
    if current is not None:
        return current
    specification = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


HARNESS = _load_harness()


def _run_id(label: str) -> str:
    return f"unit-{label}-{uuid.uuid4().hex}"


def _browser_secrets():
    runtime = HARNESS.RUNTIME.RuntimeSecrets.create()
    return HARNESS.BrowserSecrets(
        runtime=runtime,
        stop_token="prd080-unit-stop-" + "s" * 48,
        request_reason="prd080-unit-request-reason-" + "r" * 32,
        time_reason="prd080-unit-time-reason-" + "t" * 32,
        approval_reason="prd080-unit-approval-reason-" + "a" * 32,
        rejection_reason="prd080-unit-rejection-reason-" + "j" * 32,
    )


def _remove_claimed(path: Path, root: Path, owner: str) -> None:
    if not path.exists():
        return
    claim = path / HARNESS.OWNER_FILENAME
    if not claim.exists():
        claim.write_text(f"{owner}\n", encoding="ascii")
    HARNESS._remove_owned_directory(path, root, owner, stage="cleanup.unit")


def _complete_browser_facts() -> dict[str, object]:
    desktop = {
        name: True
        for name in {
            "nonblank",
            "page_112",
            "horizontal_overflow_free",
            "text_overflow_free",
            "request_scope_created",
            "time_scope_created",
            "list_rows_exact",
            "applicant_self_review_blocked",
            "request_detail_exact",
            "approve_terminal",
            "partial_adjustment",
            "approved_timeline_exact",
            "private_text_collapsed",
            "html_escape_safe",
        }
    }
    desktop.update({"width": 1280, "height": 720, "overlap_count": 0})
    mobile = {
        name: True
        for name in {
            "nonblank",
            "page_112",
            "horizontal_overflow_free",
            "text_overflow_free",
            "list_rows_exact",
            "reject_terminal",
            "rejected_timeline_exact",
            "native_dialog",
            "initial_focus",
            "tab_forward_wrap",
            "tab_reverse_wrap",
            "idle_escape_closed",
            "focus_restored",
            "busy_controls_disabled",
            "busy_escape_blocked",
            "reason_cleared",
            "decision_cleared",
            "private_text_collapsed",
        }
    }
    mobile.update({"width": 390, "height": 844, "overlap_count": 0})
    lifecycle = {
        name: True
        for name in {
            "backend_rbac_denied",
            "logout_root_cleared",
            "logout_storage_cleared",
            "logout_late_response_discarded",
            "revocation_root_cleared",
            "revocation_navigation_cleared",
            "revocation_late_response_discarded",
            "registered_private_values",
        }
    }
    return {
        "status": "passed",
        "desktop": desktop,
        "mobile": mobile,
        "lifecycle": lifecycle,
        "network": {
            "external_requests": 0,
            "forbidden_queries": 0,
            "console_errors": 0,
            "page_errors": 0,
            "login_statuses": [200, 200],
            "logout_statuses": [200],
            "create_statuses": [201, 201],
            "transition_statuses": [200, 200, 200, 200],
            "list_successes": 2,
            "detail_successes": 2,
        },
        "screenshots": list(HARNESS.SCREENSHOTS),
        "all_108_prds_complete": False,
    }


def test_boundaries_are_repository_owned_and_frozen() -> None:
    assert HARNESS.ROOT == ROOT
    assert HARNESS.WORK_ROOT == ROOT / ".tmp" / "prd080-browser-harness"
    assert HARNESS.ARTIFACT_ROOT == ROOT / ".artifacts" / "prd080-browser"
    assert HARNESS.LOOPBACK == "127.0.0.1"
    assert HARNESS.EXPECTED_MIGRATION_COUNT == 73
    assert HARNESS.EXPECTED_MIGRATION_VERSION == "0073"
    assert HARNESS.ALLOWED_ARTIFACTS == frozenset(
        (*HARNESS.SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json")
    )
    assert all(path.resolve().is_relative_to(ROOT) for path in HARNESS.SOURCE_FILES.values())


def test_run_directories_require_exact_owner_for_cleanup() -> None:
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, final = HARNESS.create_run_directories(_run_id("owner"), owner)
    try:
        assert not final.exists()
        assert (work / HARNESS.OWNER_FILENAME).read_text(encoding="ascii").strip() == owner
        assert (staging / HARNESS.OWNER_FILENAME).read_text(encoding="ascii").strip() == owner
        with pytest.raises(HARNESS.AcceptanceFailure) as wrong_owner:
            HARNESS._remove_owned_directory(
                work,
                HARNESS.WORK_ROOT,
                "0" * 64,
                stage="cleanup.unit",
            )
        assert (wrong_owner.value.stage, wrong_owner.value.code) == (
            "cleanup.unit",
            "claim",
        )
        assert work.exists()
    finally:
        _remove_claimed(staging, HARNESS.ARTIFACT_ROOT, owner)
        _remove_claimed(work, HARNESS.WORK_ROOT, owner)


def test_empty_database_reaches_0073_seeds_real_charges_and_revokes_finance() -> None:
    secrets_ = _browser_secrets()
    harness = HARNESS.BrowserHarness(_run_id("database"), secrets_)
    try:
        graph = HARNESS.prepare_database(harness.work, secrets_.runtime)
        harness.graph = graph
        assert graph.database.is_file()
        assert graph.migration == {
            "fresh": {
                "count": 73,
                "version": "0073",
                "integrity": "ok",
                "foreign_key_violations": 0,
            },
            "repeat_applied": 0,
            "seeded_charge_count": 4,
        }
        connection = HARNESS.connect_database(graph.database_url)
        try:
            users = {
                (str(row["email"]), str(row["role"]))
                for row in connection.execute(
                    "SELECT email,role FROM users WHERE tenant_id=? AND email IN(?,?,?)",
                    (
                        graph.tenant_id,
                        HARNESS.APPLICANT_EMAIL,
                        HARNESS.REVIEWER_EMAIL,
                        HARNESS.DEVELOPER_EMAIL,
                    ),
                ).fetchall()
            }
            assert users == {
                (HARNESS.APPLICANT_EMAIL, "finance_admin"),
                (HARNESS.REVIEWER_EMAIL, "finance_admin"),
                (HARNESS.DEVELOPER_EMAIL, "developer"),
            }
            charge_rows = connection.execute(
                """
                SELECT usage.request_id,charge.direction,charge.amount_micros,
                       charge.currency,charge.status
                FROM usage_ledger usage JOIN finops_ledger_entries charge
                  ON charge.tenant_id=usage.tenant_id
                 AND charge.usage_ledger_id=usage.usage_ledger_id
                WHERE usage.tenant_id=? AND charge.entry_type='usage_charge'
                  AND usage.request_id IN(?,?,?,?)
                ORDER BY usage.request_id
                """,
                (
                    graph.tenant_id,
                    *HARNESS.REQUEST_SCOPE_IDS,
                    *HARNESS.TIME_SCOPE_IDS,
                ),
            ).fetchall()
            assert {str(row["request_id"]) for row in charge_rows} == set(
                (*HARNESS.REQUEST_SCOPE_IDS, *HARNESS.TIME_SCOPE_IDS)
            )
            assert {int(row["amount_micros"]) for row in charge_rows} == {
                2_000_000,
                3_000_000,
                4_000_000,
                5_000_000,
            }
            assert {
                (str(row["direction"]), str(row["currency"]), str(row["status"]))
                for row in charge_rows
            } == {("debit", "CNY", "posted")}
            role_before = connection.execute(
                "SELECT role_id,permission_revision FROM roles "
                "WHERE tenant_id=? AND code='finance_admin'",
                (graph.tenant_id,),
            ).fetchone()
            assert role_before is not None
        finally:
            connection.close()

        harness.revoke_finance_permissions()
        assert harness.finance_revoked is True
        connection = HARNESS.connect_database(graph.database_url)
        try:
            role_after = connection.execute(
                "SELECT role_id,permission_revision FROM roles "
                "WHERE tenant_id=? AND code='finance_admin'",
                (graph.tenant_id,),
            ).fetchone()
            assert role_after is not None
            assert int(role_after["permission_revision"]) == int(
                role_before["permission_revision"]
            ) + 1
            grants = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT permission.code FROM role_permissions grant_row
                    JOIN permissions permission
                      ON permission.tenant_id=grant_row.tenant_id
                     AND permission.permission_id=grant_row.permission_id
                    WHERE grant_row.tenant_id=? AND grant_row.role_id=?
                    """,
                    (graph.tenant_id, role_after["role_id"]),
                ).fetchall()
            }
            assert not {
                "billing.dispute.read",
                "billing.dispute.create",
                "billing.dispute.review",
            } & grants
            assert "billing.finops.write" in grants
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            connection.close()
    finally:
        harness.abort()


def test_private_reason_text_is_allowed_only_in_owned_sqlite() -> None:
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, _final = HARNESS.create_run_directories(_run_id("privacy"), owner)
    private_value = "prd080-private-scan-canary-" + "x" * 32
    try:
        database = work / "prd080-browser.sqlite3"
        database.write_text(private_value, encoding="utf-8")
        (work / "logs" / "control.log").write_text("redacted\n", encoding="ascii")
        result = HARNESS._verify_private_outside_database(
            work,
            database,
            (private_value,),
        )
        assert result["plaintext_matches"] == 0
        leak = work / "spool" / "leak.bin"
        leak.write_bytes(private_value.encode("utf-8"))
        with pytest.raises(HARNESS.AcceptanceFailure) as exposed:
            HARNESS._verify_private_outside_database(work, database, (private_value,))
        assert (exposed.value.stage, exposed.value.code) == (
            "privacy.work",
            "private_value",
        )
    finally:
        _remove_claimed(staging, HARNESS.ARTIFACT_ROOT, owner)
        _remove_claimed(work, HARNESS.WORK_ROOT, owner)


def test_publish_rejects_extra_entry_then_atomically_promotes_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_ = _browser_secrets()
    harness = HARNESS.BrowserHarness(_run_id("publish"), secrets_)
    try:
        database = harness.work / "prd080-browser.sqlite3"
        database.write_bytes(b"")
        harness.graph = SimpleNamespace(database=database, database_url="sqlite:///unit")
        harness.browser_facts = _complete_browser_facts()
        harness.backend_rbac_denied = True
        harness.finance_revoked = True
        harness.registered_private_values = True
        harness.private_extra = ("prd080-unit-idempotency-" + "i" * 32,)
        for name in HARNESS.SCREENSHOTS:
            (harness.staging / name).write_bytes(b"unit-png-placeholder")
        unexpected = harness.staging / "unexpected.sqlite3"
        unexpected.write_bytes(b"must-not-publish")

        monkeypatch.setattr(HARNESS.RUNTIME, "quiesce_database", lambda _graph: None)
        monkeypatch.setattr(HARNESS, "_validate_png", lambda *_arguments: None)
        monkeypatch.setattr(
            HARNESS.RUNTIME._BASE,
            "verify_private_storage",
            lambda *_arguments: {"files_scanned": 0, "plaintext_matches": 0},
        )
        monkeypatch.setattr(
            HARNESS,
            "_verify_private_outside_database",
            lambda *_arguments: {"files_scanned": 0, "plaintext_matches": 0},
        )
        monkeypatch.setattr(HARNESS, "_source_provenance", lambda: {"unit": "0" * 64})
        monkeypatch.setattr(HARNESS.RUNTIME, "assert_safe_evidence", lambda *_arguments: None)
        monkeypatch.setattr(
            HARNESS.BrowserHarness,
            "_database_summary",
            lambda _self: {"migration": {"count": 73, "version": "0073"}},
        )

        with pytest.raises(HARNESS.AcceptanceFailure) as rejected:
            harness.publish()
        assert (rejected.value.stage, rejected.value.code) == (
            "evidence.manifest",
            "name",
        )
        assert harness.staging.is_dir() and not harness.final.exists()

        unexpected.unlink()
        evidence_path = harness.publish()
        assert evidence_path == harness.final / "browser-acceptance.json"
        assert {path.name for path in harness.final.iterdir()} == HARNESS.ALLOWED_ARTIFACTS
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        manifest = json.loads(
            (harness.final / "sha256-manifest.json").read_text(encoding="utf-8")
        )
        assert evidence["all_108_prds_complete"] is False
        assert manifest["all_108_prds_complete"] is False
        assert {entry["name"] for entry in manifest["files"]} == (
            HARNESS.ALLOWED_ARTIFACTS - {"sha256-manifest.json"}
        )
        assert not harness.work.exists() and not harness.staging.exists()
    finally:
        _remove_claimed(harness.staging, HARNESS.ARTIFACT_ROOT, harness.owner)
        _remove_claimed(harness.final, HARNESS.ARTIFACT_ROOT, harness.owner)
        _remove_claimed(harness.work, HARNESS.WORK_ROOT, harness.owner)
