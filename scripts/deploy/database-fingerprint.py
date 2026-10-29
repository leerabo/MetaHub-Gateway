"""Read-only PostgreSQL logical fingerprints for encrypted backup verification.

Only counts, schema definitions and hashes leave PostgreSQL; row values never do.
The caller must pause writers across the database/files backup snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def fingerprint(connection):
    # Enforce one repeatable, read-only snapshot, including schema discovery.
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    connection.execute("SET LOCAL timezone='UTC'")
    schema = connection.execute("SELECT current_schema()").fetchone()[0]
    tables = connection.execute("""SELECT c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind IN ('r','p') ORDER BY c.relname""", (schema,)).fetchall()
    columns = connection.execute("""SELECT table_name,column_name,data_type,udt_schema,udt_name,
        is_nullable,column_default,is_generated,generation_expression
        FROM information_schema.columns WHERE table_schema=%s ORDER BY table_name,ordinal_position""", (schema,)).fetchall()
    constraints = connection.execute("""SELECT c.relname,k.conname,pg_get_constraintdef(k.oid,true)
        FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid=k.conrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s ORDER BY c.relname,k.conname""", (schema,)).fetchall()
    indexes = connection.execute("""SELECT tablename,indexname,indexdef FROM pg_catalog.pg_indexes
        WHERE schemaname=%s ORDER BY tablename,indexname""", (schema,)).fetchall()
    functions = connection.execute("""SELECT p.proname,pg_get_function_identity_arguments(p.oid),pg_get_functiondef(p.oid)
        FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname=%s AND p.prokind IN ('f','p') ORDER BY 1,2""", (schema,)).fetchall()
    triggers = connection.execute("""SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid,true)
        FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND NOT t.tgisinternal ORDER BY 1,2""", (schema,)).fetchall()
    views = connection.execute("""SELECT c.relname,c.relkind,pg_get_viewdef(c.oid,true)
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind IN ('v','m') ORDER BY 1""", (schema,)).fetchall()
    result = {"format": 1, "schema": schema,
              "schema_sha256": digest_json([columns, constraints, indexes, functions, triggers, views]),
              "tables": {}, "sequences": {}}
    for (table,) in tables:
        digest, count = hashlib.sha256(), 0
        # Sort fixed-size row hashes in PG, allowing work_mem spill instead of
        # accumulating business rows or all row hashes in the Python process.
        with connection.cursor(name="fingerprint_" + str(len(result["tables"]))) as cursor:
            cursor.execute(sql.SQL("""SELECT encode(sha256(convert_to(to_jsonb(r)::text,'UTF8')),'hex')
                FROM {} r ORDER BY 1""").format(sql.Identifier(schema, table)))
            for (row_hash,) in cursor:
                digest.update(row_hash.encode("ascii") + b"\n")
                count += 1
        result["tables"][table] = {"rows": count, "sha256": digest.hexdigest()}
    sequences = connection.execute("""SELECT c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind='S' ORDER BY c.relname""", (schema,)).fetchall()
    for (sequence,) in sequences:
        value, called = connection.execute(sql.SQL("SELECT last_value,is_called FROM {}").format(
            sql.Identifier(schema, sequence))).fetchone()
        result["sequences"][sequence] = {"last_value": value, "is_called": called}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Only a newly created restore-check database may override the configured DB")
    parser.add_argument("--expected-stdin", action="store_true")
    args = parser.parse_args()
    try:
        # File entrypoints start with /app/tools on sys.path, while service.py
        # belongs to /app. Do not depend on the caller's current directory.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from service import load_secrets, validate_production
        import os
        load_secrets(Path("/run/secrets/runtime.env"))
        validate_production(dict(os.environ))
        url = os.environ["METAHUB_DATABASE_URL"]
        if args.database:
            if not re.fullmatch(r"restore_[0-9]{8}_[0-9]{6}_[0-9a-f]{12}", args.database):
                raise ValueError("Invalid restore-check database.")
            url = make_conninfo(url, dbname=args.database)
        with psycopg.connect(url) as connection:
            actual = fingerprint(connection)
        if args.expected_stdin:
            raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("Oversized verification manifest.")
            expected = json.loads(raw)
            if actual != expected:
                raise ValueError("Restored database differs from the backup snapshot.")
            print(json.dumps({"status": "verified", "tables": len(actual["tables"]),
                              "rows": sum(item["rows"] for item in actual["tables"].values()),
                              "schema_and_sequences_match": True}))
        else:
            print(json.dumps(actual, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as error:
        # Driver errors and manifests can contain internal configuration. Do not
        # include their values or exception messages in application/container logs.
        print(json.dumps({"status": "failed", "phase": "database_verification",
                          "exception_type": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
