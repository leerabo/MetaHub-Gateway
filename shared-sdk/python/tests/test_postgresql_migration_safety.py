"""Native migration scripts cannot escape the runner-owned transaction."""

import pytest
from metahub_shared.db import DbError
from metahub_shared.db.migrate import _require_transaction_body


@pytest.mark.parametrize("boundary", [
    "begin", "BEGIN WORK", "start transaction", "commit", "end", "rollback",
    "abort", "SAVEPOINT nested", "RELEASE SAVEPOINT nested",
    "ROLLBACK TO SAVEPOINT nested", "PREPARE TRANSACTION 'test'",
    "COMMIT PREPARED 'test'", "ROLLBACK PREPARED 'test'",
    "/* outer /* inner */ comment */ CoMmIt /* trailing */",
])
def test_native_transaction_boundaries_are_rejected(boundary):
    with pytest.raises(DbError, match="transaction boundary"):
        _require_transaction_body(f"SELECT 1; {boundary}; SELECT 2;")


@pytest.mark.parametrize("sql", [
    "-- COMMIT;\nSELECT 'BEGIN; COMMIT;';",
    "SELECT $$ COMMIT; $$, $tag$BEGIN;$tag$;",
    "DO $body$ BEGIN PERFORM 1; END; $body$;",
    "CREATE FUNCTION probe() RETURNS int LANGUAGE SQL BEGIN ATOMIC SELECT 1; END;",
    "SELECT '{}'::jsonb ? 'flag';",
])
def test_native_literals_comments_and_procedural_bodies_are_not_boundaries(sql):
    _require_transaction_body(sql)


def test_invalid_native_sql_fails_closed():
    with pytest.raises(DbError, match="cannot be parsed"):
        _require_transaction_body("CREATE TABLE (")
