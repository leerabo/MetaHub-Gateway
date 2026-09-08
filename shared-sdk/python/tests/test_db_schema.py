"""Schema discovery keeps native column order and rejects SQL identifiers as input."""

import sqlite3
from contextlib import closing

import pytest
from metahub_shared.db.schema import (
    foreign_key_violation_count,
    table_columns,
    table_exists,
    tables_exist,
)


def test_sqlite_schema_discovery_preserves_order_and_transaction():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE probe(z TEXT, a INTEGER PRIMARY KEY)')
        connection.execute('CREATE VIEW probe_view AS SELECT * FROM probe')
        connection.execute("BEGIN")
        assert table_exists(connection, "probe")
        assert not table_exists(connection, "probe_view")
        assert not table_exists(connection, "missing")
        assert table_columns(connection, "probe") == ("z", "a")
        assert table_columns(connection, "missing") == ()
        assert connection.in_transaction


def test_sqlite_table_discovery_matches_main_schema_and_tracks_ddl():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE probe(id INTEGER)")
        connection.execute("CREATE VIEW probe_view AS SELECT * FROM probe")
        connection.execute("CREATE TEMP TABLE temporary_only(id INTEGER)")
        connection.execute("CREATE TEMP VIEW probe AS SELECT 1")
        connection.execute("CREATE VIRTUAL TABLE probe_search USING fts5(content)")
        connection.execute("ATTACH DATABASE ':memory:' AS attached")
        connection.execute("CREATE TABLE attached.attached_only(id INTEGER)")
        for name in (
            "probe", "Probe", "probe_view", "temporary_only", "attached_only", "missing",
            "probe_search", "probe_search_data", "sqlite_schema",
        ):
            expected = connection.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (name,)
            ).fetchone() is not None
            assert table_exists(connection, name) == expected
            assert tables_exist(connection, (name,)) == expected
        connection.execute("DROP TABLE main.probe")
        assert not table_exists(connection, "probe")
        connection.execute("CREATE TABLE main.probe(id INTEGER)")
        assert table_exists(connection, "probe")
        assert tables_exist(connection, ("probe", "probe_search"))


@pytest.mark.parametrize("name", ['', 'probe; DROP TABLE probe', 'probe"', '../probe', None])
def test_schema_discovery_rejects_invalid_table_names(name):
    with pytest.raises(ValueError, match="Invalid schema table name"):
        table_columns(None, name)
    with pytest.raises(ValueError, match="Invalid schema table name"):
        table_exists(None, name)
    with pytest.raises(ValueError, match="Invalid schema table name"):
        tables_exist(None, ("probe", name))


def test_required_schema_set_uses_one_query_and_observes_transactional_ddl():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE first_table(id INTEGER)")
        connection.execute("CREATE TABLE second_table(id INTEGER)")
        connection.execute("BEGIN")
        statements = []
        connection.set_trace_callback(statements.append)
        assert tables_exist(connection, ("first_table", "second_table", "first_table"))
        assert len(statements) == 1
        assert connection.in_transaction
        assert tables_exist(connection, ())
        assert len(statements) == 1
        connection.set_trace_callback(None)
        connection.execute("DROP TABLE second_table")
        assert not tables_exist(connection, ("first_table", "second_table"))
        connection.rollback()
        assert tables_exist(connection, ("first_table", "second_table"))


def test_sqlite_foreign_key_check_preserves_deferred_transaction():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE parent(a INTEGER, b INTEGER, PRIMARY KEY(a,b))")
        connection.execute(
            "CREATE TABLE child(a INTEGER, b INTEGER, FOREIGN KEY(a,b) "
            "REFERENCES parent(a,b) DEFERRABLE INITIALLY DEFERRED)"
        )
        connection.execute("INSERT INTO parent VALUES(1,2)")
        connection.commit()
        connection.execute("BEGIN")
        connection.executemany(
            "INSERT INTO child VALUES(?,?)", [(1, 2), (None, 9), (9, None), (None, None)]
        )
        assert foreign_key_violation_count(connection) == 0
        connection.execute("INSERT INTO child VALUES(9,9)")
        assert foreign_key_violation_count(connection) == 1
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute("SELECT count(*) FROM child").fetchone()[0] == 0
        assert foreign_key_violation_count(connection) == 0
