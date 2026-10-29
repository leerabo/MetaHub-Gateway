"""Static safety and publication contracts for the PRD-080 runtime runner."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_runtime_module():
    path = Path(__file__).resolve().parents[1] / "prd080-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(
        "prd080_runtime_acceptance_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise AssertionError("unable to load PRD-080 runtime acceptance")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


RUNTIME = _load_runtime_module()


def test_runtime_boundaries_are_repository_owned_and_loopback_only() -> None:
    root = Path(__file__).resolve().parents[3]
    assert RUNTIME.ROOT == root
    assert RUNTIME.WORK_ROOT == root / ".tmp" / "prd080-runtime"
    assert RUNTIME.ARTIFACT_ROOT == root / ".artifacts" / "prd080-runtime"
    assert RUNTIME.PYTHON.is_relative_to(root)
    assert RUNTIME.LOOPBACK == "127.0.0.1"
    assert len(RUNTIME.FIXED_PORTS) == len(set(RUNTIME.FIXED_PORTS)) == 4
    assert RUNTIME.EXPECTED_MIGRATION_COUNT == 73
    assert RUNTIME.EXPECTED_MIGRATION_VERSION == "0073"
    assert RUNTIME.PREVIOUS_MIGRATION_VERSION == "0072"
    assert callable(RUNTIME._PROCESS.start_service)
    assert callable(RUNTIME._PROCESS.wait_health)


def test_runtime_secrets_are_unique_repr_safe_and_environment_is_bounded(
    tmp_path: Path,
) -> None:
    secrets_ = RUNTIME.RuntimeSecrets.create()
    values = secrets_.values()
    assert len(values) == len(set(values)) == 14
    assert all(len(value) >= 32 and value not in repr(secrets_) for value in values)

    work = tmp_path / "work"
    for name in ("files", "logs", "spool", "temp"):
        (work / name).mkdir(parents=True, exist_ok=True)
    graph = SimpleNamespace(database_url="sqlite:///repository-owned.sqlite3")
    environment = RUNTIME.service_environment(work, graph, secrets_)
    assert environment["TEMP"] == environment["TMP"] == str(work / "temp")
    assert environment["NO_PROXY"] == "127.0.0.1,localhost"
    assert environment["METAHUB_DATABASE_URL"] == graph.database_url
    assert environment["METAHUB_GATEWAY_API_BASE_URL"].endswith(f":{RUNTIME.GATEWAY_PORT}")
    assert environment["METAHUB_CONTROL_API_BASE_URL"].endswith(f":{RUNTIME.CONTROL_PORT}")
    for host_only_name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "OPENAI_API_KEY",
        "USERPROFILE",
    ):
        assert host_only_name not in environment


@pytest.mark.parametrize(
    "unsafe",
    ("..", "../outside", "C:/outside", "contains space", "a/b", "a\\b", "x" * 81),
)
def test_run_id_rejects_path_or_argument_injection(unsafe: str) -> None:
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.safe_run_id(unsafe)
    assert (failure.value.stage, failure.value.code) == ("config.run_id", "invalid")


def test_webhook_schema_is_exact_and_rejects_private_fields() -> None:
    payload = {
        "schema_version": 1,
        "billing_dispute_id": "bdsp_" + "a" * 32,
        "scope_type": "request_ids",
        "item_count": 2,
        "currency_totals": {"CNY": "2.400000"},
        "status": "submitted",
        "created_at": "2026-08-15T00:00:00Z",
    }
    envelope = {
        "event_id": "bdevt_" + "b" * 32,
        "event_type": "billing_dispute.created",
        "timestamp": "2026-08-15T00:00:00Z",
        "tenant_id": "tenant_public",
        "actor_id": "user_public",
        "object_id": payload["billing_dispute_id"],
        "trace_id": "trace_public",
        "payload": payload,
    }
    assert RUNTIME._valid_webhook_envelope(envelope)
    assert not RUNTIME._contains_forbidden_webhook_key(envelope)

    for private_name in (
        "reason",
        "decision_reason",
        "token",
        "prompt",
        "response",
        "provider_key",
        "endpoint_url",
    ):
        polluted = {**payload, private_name: "private-canary"}
        assert not RUNTIME._valid_webhook_payload(polluted)
        assert RUNTIME._contains_forbidden_webhook_key(polluted)


def test_webhook_recovery_requires_one_stable_delivery_id_per_event() -> None:
    receiver = RUNTIME.WebhookStub(port=RUNTIME.WEBHOOK_PORT)
    first_digest = "a" * 64
    second_digest = "b" * 64
    first_delivery = "whd_" + "1" * 32
    second_delivery = "whd_" + "2" * 32
    receiver._first_digest = first_digest
    receiver._delivery_counts = {first_digest: 3, second_digest: 1}
    receiver._delivery_ids = {
        first_digest: {first_delivery},
        second_digest: {second_delivery},
    }
    receiver._logical_digests = {first_digest, second_digest}
    receiver.network_deliveries = 4
    assert receiver.snapshot(first_delivery)["stable_delivery_id"] is True

    receiver._delivery_ids[first_digest].add("whd_" + "3" * 32)
    assert receiver.snapshot(first_delivery)["stable_delivery_id"] is False
    receiver.stop()
    assert receiver._release_crash_attempt.is_set()


def test_control_crash_targets_only_the_owned_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 31415

        def __init__(self) -> None:
            self.running = True

        def poll(self) -> int | None:
            return None if self.running else 1

    process = FakeProcess()
    control = SimpleNamespace(
        process=process,
        stdout_file=io.BytesIO(),
        stderr_file=io.BytesIO(),
    )
    processes = SimpleNamespace(items=[control])
    commands: list[list[str]] = []

    def run(command: list[str], **options: object) -> SimpleNamespace:
        commands.append(command)
        assert options.get("check") is False
        process.running = False
        return SimpleNamespace(returncode=0)

    def close_owned(item: object) -> None:
        assert item is control and process.poll() == 1
        control.stdout_file.close()
        control.stderr_file.close()

    monkeypatch.setattr(RUNTIME.sys, "platform", "win32")
    monkeypatch.setattr(RUNTIME.subprocess, "run", run)
    monkeypatch.setattr(RUNTIME._PROCESS, "stop_owned_process", close_owned)
    monkeypatch.setattr(RUNTIME, "_port_available", lambda port: port == RUNTIME.CONTROL_PORT)
    RUNTIME._crash_owned_control(processes, control)
    assert commands == [["taskkill.exe", "/PID", "31415", "/T", "/F"]]
    assert processes.items == []
    assert control.stdout_file.closed and control.stderr_file.closed


def test_evidence_is_allowlisted_and_keeps_global_incomplete() -> None:
    payload = RUNTIME.empty_evidence_payload()
    RUNTIME._validate_evidence(payload)
    assert payload["all_108_prds_complete"] is False
    assert payload["migration"] == {
        "fresh": {
            "count": 73,
            "version": "0073",
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
        "repeat": {
            "count": 73,
            "version": "0073",
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
        "down": {
            "count": 72,
            "version": "0072",
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
        "reup": {
            "count": 73,
            "version": "0073",
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
    }
    RUNTIME.assert_safe_evidence(payload, ())
    for forbidden in (
        "request_ids",
        "reason",
        "decision_reason",
        "prompt",
        "response",
        "token",
        "provider_key",
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.assert_safe_evidence({forbidden: "private"}, ())


def test_run_directories_are_exclusive_and_owner_claimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_root = tmp_path / "work-root"
    artifact_root = tmp_path / "artifact-root"
    monkeypatch.setattr(RUNTIME, "WORK_ROOT", work_root)
    monkeypatch.setattr(RUNTIME, "ARTIFACT_ROOT", artifact_root)
    owner = "a" * 64
    work, staging, final = RUNTIME.create_run_directories("prd080-unit", owner)
    assert work.parent == work_root
    assert staging.parent == artifact_root
    assert not final.exists()
    assert (work / RUNTIME.OWNER_FILENAME).read_text(encoding="ascii").strip() == owner
    assert (staging / RUNTIME.OWNER_FILENAME).read_text(encoding="ascii").strip() == owner
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.create_run_directories("prd080-unit", owner)


def test_evidence_publish_is_atomic_and_hashed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / ".staging"
    final = tmp_path / "final"
    staging.mkdir()
    payload = RUNTIME.empty_evidence_payload()
    scans: list[Path] = []

    def verify(path: Path, _private_values: tuple[str, ...]) -> dict[str, int]:
        scans.append(path)
        return {
            "files_scanned": len(tuple(path.rglob("*"))),
            "plaintext_matches": 0,
            "sqlite_sidecars_remaining": 0,
        }

    monkeypatch.setattr(RUNTIME._BASE, "scan_private_storage", verify)
    target = RUNTIME.write_evidence(staging, final, payload, ("private-canary",))
    assert target == final / "runtime-acceptance.json"
    assert final.is_dir() and not staging.exists()
    assert scans == [staging]
    encoded = target.read_bytes()
    assert json.loads(encoded) == payload
    assert (final / "SHA256SUMS.txt").read_text(encoding="ascii") == (
        f"{hashlib.sha256(encoded).hexdigest()}  runtime-acceptance.json\n"
    )


def test_source_provenance_binds_runtime_and_recovery_implementations() -> None:
    provenance = RUNTIME.source_provenance()
    assert set(provenance) == set(RUNTIME.SOURCE_FILES)
    for name, path in RUNTIME.SOURCE_FILES.items():
        assert provenance[name] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert len(provenance[name]) == 64


def test_main_never_echoes_private_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_argument = "private-token-value"
    monkeypatch.setattr(
        RUNTIME,
        "run",
        lambda _run_id: (_ for _ in ()).throw(
            RUNTIME.AcceptanceFailure("database.business_seed", "failed")
        ),
    )
    assert RUNTIME.main(["--run-id", private_argument]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("PRD-080 runtime acceptance failed: database.business_seed:failed\n")
    assert private_argument not in captured.err
