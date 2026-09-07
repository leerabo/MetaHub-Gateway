"""Actual process and TLS sockets with the full Control app; not Docker proof."""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metahub_assurance_runner.lease_channel import (
    LeaseBinding,
    StreamLeaseTransport,
    SupervisorLeaseSession,
)
from metahub_assurance_runner.probes import IMPLEMENTED_SCENARIOS, RESOURCE_SCENARIOS, SCENARIOS
from metahub_shared.db.migrate import apply_migrations
from metahub_shared.db.seed import apply_seed_data
from metahub_shared.files.storage import file_object_key

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def guest_baseline(tmp_path_factory):
    target = tmp_path_factory.mktemp("guest-base") / "base.sqlite3"
    supplied = os.environ.get("METAHUB_TEST_MIGRATED_BASELINE")
    if supplied:
        shutil.copyfile(Path(supplied).resolve(strict=True), target)
    apply_migrations(f"sqlite:///{target.as_posix()}")
    if not supplied:
        apply_seed_data(f"sqlite:///{target.as_posix()}")
    return target


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "revoked_before_start",
        "revoked_during_bootstrap",
        "revoked_during_resources",
        "tampered_object",
    ],
)
async def test_fixed_guest_real_process_https_and_report(
    guest_baseline,
    tmp_path,
    mode,
    record_property,
):
    baseline_before = hashlib.sha256(guest_baseline.read_bytes()).hexdigest()
    with closing(sqlite3.connect(guest_baseline.as_uri() + "?mode=ro", uri=True)) as database:
        baseline_registrations = database.execute(
            "SELECT COUNT(*) FROM registration_intents"
        ).fetchone()[0]
        baseline_files = {row[0] for row in database.execute("SELECT file_id FROM uploaded_files")}
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
        if key in os.environ
    }
    environment.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": os.pathsep.join(
                str(ROOT / part)
                for part in (
                    "control-plane-api/src",
                    "shared-sdk/python",
                    "gateway-data-plane/src",
                )
            ),
            "METAHUB_CONFIG_FILE": "must-not-be-read.env",
            "METAHUB_DATABASE_URL": "must-not-be-used",
            "HTTPS_PROXY": "http://127.0.0.1:1",
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "control-plane-api/tests/fixtures/prd137_guest_process.py"),
        str(guest_baseline),
        str(tmp_path),
        str(ROOT / "infra/migrations"),
        cwd=ROOT,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    calls = 0
    observed_creation = False
    observed_resource = False
    injected_fault = False

    async def authority():
        nonlocal calls, observed_creation, observed_resource, injected_fault
        calls += 1
        if mode == "revoked_before_start":
            return calls < 2
        if mode == "revoked_during_bootstrap":
            for target in tmp_path.glob("metahub-assurance-*/control.sqlite3"):
                try:
                    with closing(
                        sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=0.5)
                    ) as database:
                        count = database.execute(
                            "SELECT COUNT(*) FROM registration_intents"
                        ).fetchone()[0]
                    if count > baseline_registrations:
                        observed_creation = True
                        return False
                except sqlite3.OperationalError:
                    pass
        if mode in ("revoked_during_resources", "tampered_object") and not injected_fault:
            for target in tmp_path.glob("metahub-assurance-*/control.sqlite3"):
                try:
                    with closing(
                        sqlite3.connect(
                            target.as_uri() + "?mode=ro",
                            uri=True,
                            timeout=0.1,
                        )
                    ) as database:
                        created = [
                            row[0]
                            for row in database.execute(
                                "SELECT file_id FROM uploaded_files WHERE status='ready' LIMIT 20"
                            )
                            if row[0] not in baseline_files
                        ]
                    if created:
                        observed_resource = True
                        if mode == "revoked_during_resources":
                            return False
                        blob = (
                            target.parent / "file-storage" / file_object_key(created[0], "original")
                        )
                        assert blob.resolve(strict=True).is_relative_to(tmp_path.resolve())
                        ciphertext = blob.read_bytes()
                        blob.write_bytes(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
                        injected_fault = True
                except sqlite3.OperationalError:
                    pass
        return True

    session = SupervisorLeaseSession(
        StreamLeaseTransport(process.stdout, process.stdin),
        LeaseBinding(
            request_sha256="1" * 64,
            environment_id="aenv_" + "2" * 32,
            assessment_generation=1,
            deadline_at=datetime.now(UTC) + timedelta(minutes=3),
            channel_nonce="3" * 64,
        ),
        authority,
    )
    try:
        result = await asyncio.wait_for(session.serve(), 180)
        await asyncio.wait_for(process.wait(), 10)
        diagnostic = (await process.stderr.read()).decode("utf-8")
        if mode in ("revoked_during_bootstrap", "revoked_during_resources"):
            assert observed_creation if mode == "revoked_during_bootstrap" else observed_resource
            assert diagnostic.splitlines()[0] == (
                "BootstrapError" if mode == "revoked_during_bootstrap" else "LeaseChannelError"
            ), diagnostic
        else:
            assert not diagnostic, diagnostic
        assert hashlib.sha256(guest_baseline.read_bytes()).hexdigest() == baseline_before
        assert not list(tmp_path.iterdir())
        if mode.startswith("revoked_"):
            assert process.returncode == 1 and result.report_bytes is None
            record_property("revocation_after_observed_creation", observed_creation)
            record_property("revocation_after_observed_resource", observed_resource)
            return
        assert process.returncode == 0
        assert result.reason_code == "report_received", result
        report = json.loads(result.report_bytes)
        assert report["schema_version"] == "metahub.assurance-guest-report.v2"
        assert report["profile"] == "sqlite_https_v1"
        assert report["binding"] == session.binding.model_dump(mode="json")
        assert report["baseline_sha256"] == baseline_before
        record_property("guest_report", result.report_bytes.decode("utf-8"))
        assert not report["isolation_attested"] and not report["cleanup_attested"]
        assert not report["full_owner_coverage"]
        assert [run["scenario_code"] for run in report["runs"]] == list(SCENARIOS)
        assert len(report["creations"]) == 13
        assert len(report["resources"]) == (8 if mode == "tampered_object" else 12)
        assert {item["resource_code"] for item in report["resources"]} == (
            {"cached_projection", "metric_bucket"}
            if mode == "tampered_object"
            else {"cached_projection", "object_artifact", "metric_bucket"}
        )
        assert all(passed for item in report["resources"] for _, passed in item["checks"])
        for run in report["runs"]:
            if mode == "tampered_object" and run["scenario_code"] == "object_prefix":
                assert observed_resource and injected_fault
                assert run["outcome"] == "failed"
                assert run["reason_code"] == "resource_contract_violation"
                continue
            assert run["outcome"] == (
                "passed"
                if run["scenario_code"] in (*IMPLEMENTED_SCENARIOS, *RESOURCE_SCENARIOS)
                else "error"
            ), run
        assert b"example.test" not in result.report_bytes and b"Pr0be!" not in result.report_bytes
        assert b"__Host-" not in result.report_bytes and b"must-not-be" not in result.report_bytes
        assert b"Assurance source object" not in result.report_bytes
        record_property("storage_fault_injected", injected_fault)
    finally:
        session.close()
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 10)
