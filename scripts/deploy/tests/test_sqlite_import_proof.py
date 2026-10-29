import importlib.util
from pathlib import Path
from decimal import Decimal
import sqlite3
import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("import_proof", ROOT / "scripts/deploy/sqlite-to-postgres.py")
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


def test_equivalent_database_numeric_types_match_without_rounding():
    assert proof.digest([(80, 1, 0.5)]) == proof.digest([(Decimal("80.0"), 1.0, Decimal(".5"))])
    assert proof.digest([(Decimal("0.1234567890123456789"),)]) != proof.digest([(Decimal("0.1234567890123456788"),)])
    assert proof.digest([(1,)]) != proof.digest([("1",)])
    assert proof.digest([(Decimal("0.1234567890123456789012345678901"),)]) != proof.digest([(Decimal("0.1234567890123456789012345678902"),)])


def test_schema_proof_ignores_comments_and_spacing_but_preserves_values():
    assert proof.schema_tokens("CREATE TABLE a(x INTEGER CHECK(x > 0)); -- comment") == proof.schema_tokens("create table a (x integer check ( x > 0 ));")
    assert proof.schema_tokens("CHECK(kind='active')") != proof.schema_tokens("CHECK(kind='ACTIVE')")
    assert proof.schema_tokens("CHECK(n > 0)") != proof.schema_tokens("CHECK(n >= 0)")


def test_generated_values_and_implicit_subscription_ids_are_preserved():
    with sqlite3.connect(":memory:") as source:
        source.execute("CREATE TABLE invocations(value INTEGER, doubled INTEGER GENERATED ALWAYS AS(value*2))")
        source.execute("INSERT INTO invocations(value) VALUES(7)")
        columns = proof.source_columns(source, "invocations", {"value", "doubled"})
        assert source.execute("SELECT " + ",".join(columns) + " FROM invocations").fetchone() == (7, 14)
        source.execute("CREATE TABLE subscription_order_records(id TEXT)")
        source.execute("INSERT INTO subscription_order_records(rowid,id) VALUES(42,'own-test')")
        columns = proof.source_columns(source, "subscription_order_records", {"id", "rowid"})
        assert source.execute("SELECT " + ",".join(columns) + " FROM subscription_order_records").fetchone() == ("own-test", 42)
        with pytest.raises(ValueError, match="Column mismatch"):
            proof.source_columns(source, "invocations", {"value"})


def test_importer_finds_the_container_layout_and_rejects_incomplete_packages(tmp_path):
    app = tmp_path / "app"
    (app / "infra/migrations").mkdir(parents=True)
    (app / "shared-sdk/python").mkdir(parents=True)
    assert proof.deployment_root(app / "tools/sqlite-to-postgres.py") == app
    with pytest.raises(RuntimeError, match="missing"):
        proof.deployment_root(tmp_path / "unrelated/tools/import.py")
