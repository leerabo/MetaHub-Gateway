"""The shared commercial baseline must never replace existing or external files."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "s2-resource-api.py"
SPEC = importlib.util.spec_from_file_location("s2_commercial_runner", SCRIPT)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


@pytest.mark.parametrize("target_kind", ["external", "existing", "postgres", "query"])
def test_commercial_fixture_rejects_unowned_or_existing_target(tmp_path, target_kind):
    work = tmp_path / "run"
    destination = work / "tests" / "case" / "database.sqlite3"
    if target_kind == "external":
        destination = tmp_path / "external.sqlite3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if target_kind in {"external", "existing"}:
        destination.write_bytes(b"preserve-existing")
    database_url = "sqlite:///" + destination.as_posix()
    if target_kind == "postgres":
        database_url = "postgresql://invalid.example.test/database"
    elif target_kind == "query":
        database_url += "?mode=rw"
    with pytest.raises((ValueError, FileExistsError)):
        RUNNER.prepare_commercial_database(
            database_url, work=work, baseline=tmp_path / "unused-baseline",
            migrations_dir=tmp_path / "unused-migrations",
        )
    if target_kind in {"external", "existing"}:
        assert destination.read_bytes() == b"preserve-existing"
    else:
        assert not destination.exists()


def test_commercial_fixture_keeps_real_migration_validation(monkeypatch, tmp_path):
    from metahub_shared import db

    work = tmp_path / "run"
    baseline = tmp_path / "empty-schema"
    baseline.write_bytes(b"verified-schema")
    target = work / "tests" / "case" / "database.sqlite3"
    migrations = tmp_path / "migrations"
    url = "sqlite:///" + target.as_posix()
    calls = []

    def apply_migrations(database_url, *, migrations_dir):
        calls.append((database_url, migrations_dir))
        assert target.read_bytes() == baseline.read_bytes()
        raise ValueError("migration checksum mismatch")

    monkeypatch.setattr(db, "apply_migrations", apply_migrations)
    with pytest.raises(ValueError, match="migration checksum mismatch"):
        RUNNER.prepare_commercial_database(
            url, work=work, baseline=baseline, migrations_dir=migrations,
        )
    assert calls == [(url, migrations)]
    assert baseline.read_bytes() == b"verified-schema"
