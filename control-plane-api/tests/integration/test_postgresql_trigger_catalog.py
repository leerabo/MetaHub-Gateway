"""Inspect current attached functions after every production migration.

The result is an inventory, not proof that the listed guards are compatible.
Never inspect customer databases or serialize customer rows.
"""

import json
import os
from contextlib import closing
from pathlib import Path

import test_postgresql_native_migrations as native
from metahub_shared.db import connect_database
from test_postgresql_background_connection_lifecycle import (
    migrated_template as migrated_template,
)
from test_postgresql_background_connection_lifecycle import (
    postgresql_database as postgresql_database,
)

pytestmark = native.pytestmark


def test_inventory_current_attached_physical_identity_guards(postgresql_database):
    with closing(connect_database(postgresql_database)) as connection:
        records = connection.execute(
            "SELECT t.tgname AS trigger_name,c.relname AS table_name,t.tgtype,"
            "p.proname AS function_name,pg_get_functiondef(p.oid) AS definition,"
            "pg_get_triggerdef(t.oid) AS trigger_definition,"
            "ARRAY(SELECT a.attname FROM pg_index i "
            "JOIN unnest(i.indkey) WITH ORDINALITY k(attnum,n) ON true "
            "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum "
            "WHERE i.indrelid=c.oid AND i.indisprimary ORDER BY k.n) AS primary_key "
            "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_proc p ON p.oid=t.tgfoid "
            "WHERE n.nspname='public' AND NOT t.tgisinternal "
            "AND lower(p.prosrc) LIKE '%ctid%' ORDER BY c.relname,t.tgname"
        ).fetchall()
        rows = [dict(row) for row in records]
        assert rows, "Full-schema catalog must contain the known guard inventory"
        assert not any(row["function_name"] == "fn_trg_assistant_audits_update_prd077"
                       for row in rows), "0193 must retain its repaired function"
        destination = os.environ.get("METAHUB_TEST_TRIGGER_AUDIT_PATH")
        if destination:
            Path(destination).write_text(
                json.dumps({"status": "inventory_only", "guards": rows}, indent=2),
                encoding="utf-8",
            )
