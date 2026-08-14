"""Contracts for the bounded PRD-079 runtime acceptance runner."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_runtime_module():
    """Load the real script without starting a listener or child process."""

    path = Path(__file__).resolve().parents[1] / "prd079-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(
        "prd079_runtime_acceptance_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise AssertionError("unable to load PRD-079 runtime acceptance")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


RUNTIME = load_runtime_module()


def test_runtime_boundaries_are_repository_owned_and_loopback_only() -> None:
    root = Path(__file__).resolve().parents[3]
    assert RUNTIME.ROOT == root
    assert RUNTIME.TMP_ROOT == root / ".tmp"
    assert RUNTIME.ARTIFACT_ROOT == root / ".artifacts" / "prd079-runtime"
    assert RUNTIME.PYTHON.is_relative_to(root)
    assert RUNTIME.LOOPBACK == "127.0.0.1"
    assert len(RUNTIME.FIXED_PORTS) == len(set(RUNTIME.FIXED_PORTS)) == 3
    assert RUNTIME.EXPECTED_MIGRATION_COUNT == 72
    assert RUNTIME.EXPECTED_MIGRATION_VERSION == "0072"


def test_runtime_secrets_are_unique_and_repr_safe() -> None:
    secrets_ = RUNTIME.RuntimeSecrets.create()
    values = secrets_.values()
    assert len(values) == len(set(values))
    assert all(len(value) >= 32 for value in values)
    assert all(value not in repr(secrets_) for value in values)


@pytest.mark.parametrize(
    "unsafe",
    ("..", "../outside", "C:/outside", "contains space", "a/b", "a\\b", "x" * 81),
)
def test_run_id_rejects_path_and_argument_injection(unsafe: str) -> None:
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.safe_run_id(unsafe)
    assert (failure.value.stage, failure.value.code) == ("config.run_id", "invalid")


def test_temporary_environment_restores_present_and_missing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRD079_EXISTING", "before")
    monkeypatch.delenv("PRD079_MISSING", raising=False)
    with pytest.raises(RuntimeError, match="synthetic"):
        with RUNTIME.temporary_environment(
            {"PRD079_EXISTING": "during", "PRD079_MISSING": "created"}
        ):
            assert os.environ["PRD079_EXISTING"] == "during"
            assert os.environ["PRD079_MISSING"] == "created"
            raise RuntimeError("synthetic")
    assert os.environ["PRD079_EXISTING"] == "before"
    assert "PRD079_MISSING" not in os.environ


def test_run_directories_are_exclusive_direct_children_with_owner_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_root = tmp_path / "tmp"
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setattr(RUNTIME, "TMP_ROOT", tmp_root)
    monkeypatch.setattr(RUNTIME, "ARTIFACT_ROOT", artifact_root)
    owner = "a" * 64
    work, staging, final = RUNTIME.create_run_directories("contract-run", owner)
    assert work == tmp_root / "prd079-runtime-contract-run"
    assert staging == artifact_root / ".contract-run.staging"
    assert final == artifact_root / "contract-run"
    assert not final.exists()
    assert (work / RUNTIME.OWNER_FILENAME).read_text(encoding="ascii").strip() == owner
    assert (staging / RUNTIME.OWNER_FILENAME).read_text(encoding="ascii").strip() == owner
    assert {item.name for item in work.iterdir()} == {
        RUNTIME.OWNER_FILENAME,
        "files",
        "logs",
        "spool",
        "temp",
    }
    with pytest.raises(RUNTIME.AcceptanceFailure) as duplicate:
        RUNTIME.create_run_directories("contract-run", owner)
    assert (duplicate.value.stage, duplicate.value.code) == ("config.work", "exists")


def test_owned_work_cleanup_refuses_wrong_claim_then_removes_exact_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(RUNTIME, "TMP_ROOT", tmp_path / "tmp")
    monkeypatch.setattr(RUNTIME, "ARTIFACT_ROOT", tmp_path / "artifacts")
    owner = "b" * 64
    work, _staging, _final = RUNTIME.create_run_directories("cleanup-run", owner)
    with pytest.raises(RUNTIME.AcceptanceFailure) as wrong_owner:
        RUNTIME.remove_owned_work(work, "c" * 64)
    assert (wrong_owner.value.stage, wrong_owner.value.code) == (
        "cleanup.work",
        "claim",
    )
    assert work.exists()
    RUNTIME.remove_owned_work(work, owner)
    assert not work.exists()


def test_service_environment_is_minimal_repository_owned_and_proxy_free(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    for name in ("files", "logs", "spool", "temp"):
        (work / name).mkdir(parents=True, exist_ok=True)
    graph = SimpleNamespace(database_url="sqlite:///repository-owned.sqlite3")
    secrets_ = RUNTIME.RuntimeSecrets.create()
    environment = RUNTIME.service_environment(work, graph, secrets_)
    assert environment["TEMP"] == environment["TMP"] == str(work / "temp")
    assert environment["NO_PROXY"] == "127.0.0.1,localhost"
    assert environment["METAHUB_DATABASE_URL"] == graph.database_url
    assert environment["METAHUB_GATEWAY_API_BASE_URL"].startswith("http://127.0.0.1:")
    assert environment["METAHUB_CONTROL_API_BASE_URL"].startswith("http://127.0.0.1:")
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


def test_prepare_database_runs_72_repeat_down_71_and_reup(tmp_path: Path) -> None:
    work = tmp_path / "database"
    for name in ("files", "logs", "spool", "temp"):
        (work / name).mkdir(parents=True, exist_ok=True)
    secrets_ = RUNTIME.RuntimeSecrets.create()
    graph = RUNTIME.prepare_database(
        work,
        secrets_,
        provider_url=f"http://{RUNTIME.LOOPBACK}:{RUNTIME.PROVIDER_PORT}/v1",
    )
    assert graph.migration["fresh"] == {
        "count": 72,
        "version": "0072",
        "integrity": "ok",
        "foreign_key_violations": 0,
    }
    assert graph.migration["repeat"] == graph.migration["fresh"]
    assert graph.migration["down"] == {
        "count": 71,
        "version": "0071",
        "integrity": "ok",
        "foreign_key_violations": 0,
    }
    assert graph.migration["reup"] == graph.migration["fresh"]

    connection = RUNTIME.connect_database(graph.database_url)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        credential = connection.execute(
            "SELECT encrypted_key FROM provider_credentials WHERE tenant_id=?",
            (graph.tenant_id,),
        ).fetchone()
        assert credential is not None
        assert str(credential[0]).startswith("aesgcm-v1:")
        assert secrets_.provider_key not in str(credential[0])
    finally:
        connection.close()
    RUNTIME.quiesce_database(graph)
    privacy = RUNTIME.scan_private_storage(work, secrets_.values())
    assert privacy["plaintext_matches"] == 0
    assert privacy["sqlite_sidecars_remaining"] == 0


def test_evidence_rejects_private_keys_and_nested_private_values() -> None:
    private_value = "PRD079-private-evidence-canary"
    safe = {"status": "passed", "all_108_prds_complete": False, "counts": [1, 2]}
    RUNTIME.assert_safe_evidence(safe, (private_value,))

    with pytest.raises(RUNTIME.AcceptanceFailure) as key_failure:
        RUNTIME.assert_safe_evidence({"nested": {"provider_key": "hidden"}}, ())
    assert (key_failure.value.stage, key_failure.value.code) == (
        "evidence.shape",
        "forbidden_key",
    )
    with pytest.raises(RUNTIME.AcceptanceFailure) as value_failure:
        RUNTIME.assert_safe_evidence({"nested": [f"prefix-{private_value}"]}, (private_value,))
    assert (value_failure.value.stage, value_failure.value.code) == (
        "evidence.privacy",
        "private_value",
    )


def test_evidence_publish_is_atomic_hashed_and_keeps_global_incomplete(
    tmp_path: Path,
) -> None:
    staging = tmp_path / ".staging"
    final = tmp_path / "final"
    staging.mkdir()
    payload = RUNTIME.empty_evidence_payload()
    target = RUNTIME.write_evidence(staging, final, payload, ())
    assert target == final / "runtime-acceptance.json"
    assert final.is_dir() and not staging.exists()
    encoded = target.read_bytes()
    assert json.loads(encoded)["all_108_prds_complete"] is False
    assert (final / "SHA256SUMS.txt").read_text(encoding="ascii") == (
        f"{hashlib.sha256(encoded).hexdigest()}  runtime-acceptance.json\n"
    )


def test_main_uses_fixed_failure_coordinates_without_echoing_arguments(
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
    assert captured.err == (
        "PRD-079 runtime acceptance failed: database.business_seed:failed\n"
    )
    assert private_argument not in captured.err


def test_source_provenance_binds_runtime_migration_and_quota_recovery() -> None:
    provenance = RUNTIME.source_provenance()
    assert set(provenance) == set(RUNTIME.SOURCE_FILES)
    for name, path in RUNTIME.SOURCE_FILES.items():
        assert provenance[name] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert len(provenance[name]) == 64
