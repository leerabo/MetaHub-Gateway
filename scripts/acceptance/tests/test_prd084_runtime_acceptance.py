from __future__ import annotations

import hashlib
import importlib.util
import re
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "acceptance" / "prd084-runtime-acceptance.py"


def _load():
    name = "_metahub_prd084_runtime_acceptance_tests"
    current = sys.modules.get(name)
    if current is not None:
        return current
    specification = importlib.util.spec_from_file_location(name, SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


module = _load()


def _response(status: int, code: str | None = None) -> httpx.Response:
    body = {"code": code} if code is not None else {"status": "ok"}
    return httpx.Response(status, json=body)


def test_control_retry_is_bounded_to_explicit_public_codes(monkeypatch) -> None:
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    responses = iter([_response(503, "rbac.unavailable"), _response(200)])
    calls = 0

    def operation() -> httpx.Response:
        nonlocal calls
        calls += 1
        return next(responses)

    assert module.transient_control_call(operation).status_code == 200
    assert calls == 2

    calls = 0

    def unsafe_operation() -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(503, "private.error")

    assert module.transient_control_call(unsafe_operation).status_code == 503
    assert calls == 1


def test_budget_retry_requires_the_call_site_allowlist(monkeypatch) -> None:
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    responses = iter([_response(503, "budget.status_unavailable"), _response(201)])
    calls = 0

    def operation() -> httpx.Response:
        nonlocal calls
        calls += 1
        return next(responses)

    result = module.transient_control_call(
        operation,
        retry_codes=frozenset({"rbac.unavailable", "budget.status_unavailable"}),
    )
    assert result.status_code == 201
    assert calls == 2


def test_evidence_rejects_forbidden_keys_and_sensitive_values() -> None:
    safe = {
        "prd_id": "PRD-084",
        "status": "passed",
        "all_108_prds_complete": False,
        "source_provenance": {"runner": "a" * 64},
    }
    assert '"all_108_prds_complete": false' in module.serialize_safe_evidence(safe, ())
    for key in module.FORBIDDEN_EVIDENCE_KEYS:
        with pytest.raises(module.AcceptanceFailure):
            module.serialize_safe_evidence({key: "redacted"}, ())
    with pytest.raises(module.AcceptanceFailure):
        module.serialize_safe_evidence(safe, ("PRD-084",))


def test_source_provenance_includes_the_runner_and_only_sha256() -> None:
    provenance = module.source_hashes()
    assert "acceptance" in provenance
    assert set(provenance) == {"acceptance", "control", "events", "gateway", "migration"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in provenance.values())


def test_evidence_manifest_hashes_the_exact_persisted_bytes(tmp_path: Path) -> None:
    serialized = '{"status":"passed"}\n'
    target, digest = module.publish_evidence(
        serialized,
        artifact_root=tmp_path,
        run_id="contract",
    )

    assert target.read_bytes() == serialized.encode("utf-8")
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
    assert (tmp_path / "contract.sha256").read_text(encoding="ascii") == (
        f"{digest}  contract.json\n"
    )


def test_file_scan_maps_read_failures_to_a_fixed_acceptance_code(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "runtime.db"
    target.touch()

    def fail_read(_path: Path) -> bytes:
        raise PermissionError("sensitive operating system detail")

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(module.AcceptanceFailure) as captured:
        module.scan_files([target], ["canary"])

    assert captured.value.stage == "privacy"
    assert captured.value.code == "file_read_failed"
    assert str(captured.value) == "privacy:file_read_failed"


def test_event_settlement_closes_its_database_connection(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "settlement.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE local_model_call_event_outbox(status TEXT NOT NULL);
        CREATE TABLE webhook_deliveries(event_type TEXT NOT NULL,status TEXT NOT NULL);
        INSERT INTO local_model_call_event_outbox VALUES('sent'),('sent'),('sent');
        INSERT INTO webhook_deliveries VALUES
          ('local_model.called','success'),
          ('local_model.called','success'),
          ('local_model.called','success');
        """
    )
    connection.commit()
    closed = False

    class TrackedConnection:
        def execute(self, statement: str):
            return connection.execute(statement)

        def close(self) -> None:
            nonlocal closed
            closed = True
            connection.close()

    monkeypatch.setattr(module, "connect_database", lambda _url: TrackedConnection())
    module.wait_local_event_settlement(f"sqlite:///{database.as_posix()}", 3)
    assert closed is True


def test_runner_declares_all_real_process_phases_and_price_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for phase in ("control-config", "gateway-calls", "control-policy", "gateway-policy"):
        assert phase in source
    assert '"/model-prices"' in source
    assert '"prd084-runtime-price-v1"' in source
    assert "wait_local_event_settlement(database_url, 3)" in source
