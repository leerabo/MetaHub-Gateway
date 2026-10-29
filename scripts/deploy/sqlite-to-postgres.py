"""Copy a read-only SQLite snapshot into a fresh, migrated PostgreSQL database.

Run offline as the dedicated PostgreSQL administrator over its private Unix
socket. Source passwords, ciphertext and identifiers are copied without changes.
The destination must have no accounts, tenants, KEYs or usage. The entire import,
foreign-key checks and row digest verification commit as one transaction.
"""
from __future__ import annotations

import argparse
from decimal import Context, Decimal
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import shutil
import sys
import tempfile

import psycopg
from psycopg import sql
from dotenv import dotenv_values

def deployment_root(script: Path) -> Path:
    # The source checkout uses scripts/deploy; the runtime image uses /app/tools.
    # Locate the actual shipped migrations rather than assuming one layout.
    for candidate in script.resolve().parents[1:3]:
        if (candidate / "infra/migrations").is_dir() and (candidate / "shared-sdk/python").is_dir():
            return candidate
    raise RuntimeError("Deployment migrations and shared SDK are missing.")


ROOT = deployment_root(Path(__file__))
sys.path.insert(0, str(ROOT / "shared-sdk/python"))
EXCLUDED = {"schema_migrations", "sqlite_sequence"}


def canonical(value):
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        decimal = Decimal(str(value))
        # Decimal.normalize uses the active (usually 28-digit) context. Never
        # round a business amount while proving that its exact value survived.
        exact = decimal.normalize(context=Context(prec=max(1, len(decimal.as_tuple().digits))))
        return {"number": str(exact)}
    if isinstance(value, (bytes, memoryview)):
        return {"bytes": bytes(value).hex()}
    return value


def digest(rows) -> tuple[int, str]:
    encoded = sorted(json.dumps([canonical(value) for value in row], ensure_ascii=False, separators=(",", ":")).encode() for row in rows)
    checksum = hashlib.sha256()
    for row in encoded:
        checksum.update(len(row).to_bytes(8, "big"))
        checksum.update(row)
    return len(encoded), checksum.hexdigest()


def source_columns(connection, table, expected_columns):
    columns = [row[1] for row in connection.execute('PRAGMA table_xinfo("' + table.replace('"', '""') + '")') if row[6] != 1]
    if table == "subscription_order_records" and expected_columns - set(columns) == {"rowid"}:
        columns.append("rowid")
    if set(columns) != expected_columns:
        raise ValueError(f"Column mismatch in {table}; import rolled back.")
    return columns


def schema_tokens(text):
    # Preserve quoted values and identifiers; ignore formatting and comments only.
    pattern = r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\[(?:[^\]])*\]|`(?:``|[^`])*`|--[^\n]*|/\*[\s\S]*?\*/|[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?|\S"
    return tuple(token if token[0] in "'\"[`" else token.lower()
                 for token in re.findall(pattern, text or "")
                 if not token.startswith(("--", "/*")))


def verify_current_sqlite_schema(snapshot: Path):
    """Prove a current source's schema against a fresh canonical SQLite database.

    This route does not rewrite migration history or execute changed historical
    SQL on business rows. A history difference is reported, never concealed.
    """
    from metahub_shared.db import apply_migrations
    canonical_path = snapshot.with_name(snapshot.name + ".canonical.sqlite3")
    if canonical_path.exists():
        raise ValueError("Canonical schema proof path must be new.")
    with sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True) as actual:
        source_version = actual.execute("SELECT max(version) FROM schema_migrations WHERE status='applied'").fetchone()[0]
    if not source_version or not re.fullmatch(r"\d{4}", source_version):
        raise ValueError("Source has no valid applied migration history.")
    migrations = sorted(path for path in (ROOT / "infra/migrations").glob("*.sql") if re.match(r"\d{4}_", path.name) and not path.name.endswith(".down.sql"))
    # Reconstruct exactly the source's prefix before applying later migrations.
    # Historical checksums remain intact in the copy; no old SQL is replayed on
    # its business rows to accommodate a reported checksum difference.
    proof_directory = Path(tempfile.mkdtemp(prefix="schema-proof-", dir=snapshot.parent))
    prefix, suffix = proof_directory / "prefix", proof_directory / "suffix"
    prefix.mkdir(); suffix.mkdir()
    for path in migrations:
        shutil.copyfile(path, (prefix if path.name[:4] <= source_version else suffix) / path.name)
    apply_migrations("sqlite:///" + canonical_path.resolve().as_posix(), migrations_dir=prefix)
    with sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True) as actual, sqlite3.connect(canonical_path.resolve().as_uri() + "?mode=ro", uri=True) as expected:
        def objects(connection):
            return {(kind, name, table): schema_tokens(statement)
                    for kind, name, table, statement in connection.execute(
                        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL")}
        observed, required = objects(actual), objects(expected)
        mismatched = sorted(key[1] for key in set(observed) | set(required) if observed.get(key) != required.get(key))
        if mismatched:
            raise ValueError("Current source schema differs from the canonical schema: " + ",".join(mismatched[:8]))
        old = {row[0]: (row[1], row[2], row[3]) for row in actual.execute("SELECT version,name,checksum,status FROM schema_migrations")}
        new = {row[0]: (row[1], row[2], row[3]) for row in expected.execute("SELECT version,name,checksum,status FROM schema_migrations")}
        if set(old) != set(new) or any(old[version][0] != new[version][0] or old[version][2] != "applied" for version in new):
            raise ValueError("Source migration versions are not fully current and applied.")
        differences = [{"version": version, "source_checksum": old[version][1], "canonical_checksum": new[version][1]}
                       for version in sorted(new) if old[version][1] != new[version][1]]
        proof = {"canonical_schema_verified": True, "schema_objects": len(required), "source_history_differences": differences, "source_history_rewritten": False, "source_version": source_version}
    result = apply_migrations("sqlite:///" + snapshot.resolve().as_posix(), migrations_dir=suffix)
    proof["copy_applied_migrations"] = result.applied
    return proof


def verify_foreign_keys(connection) -> None:
    keys = connection.execute("""
        SELECT ns.nspname, t.relname, pns.nspname, p.relname, c.conkey, c.confkey, c.confmatchtype
        FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
        JOIN pg_namespace ns ON ns.oid=t.relnamespace
        JOIN pg_class p ON p.oid=c.confrelid JOIN pg_namespace pns ON pns.oid=p.relnamespace
        WHERE c.contype='f' AND ns.nspname='public'
    """).fetchall()
    for schema, table, parent_schema, parent, child_ids, parent_ids, match in keys:
        def columns(s, t, ids):
            result = dict(connection.execute("SELECT attnum,attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0", (f'"{s}"."{t}"',)).fetchall())
            return [result[key] for key in ids]
        children, parents = columns(schema, table, child_ids), columns(parent_schema, parent, parent_ids)
        equal = sql.SQL(" AND ").join(sql.SQL("c.{} = p.{}").format(sql.Identifier(a), sql.Identifier(b)) for a, b in zip(children, parents, strict=True))
        nonnull = sql.SQL(" AND ").join(sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(a)) for a in children)
        query = sql.SQL("SELECT 1 FROM {} c WHERE ({}) AND NOT EXISTS (SELECT 1 FROM {} p WHERE {}) LIMIT 1").format(sql.Identifier(schema, table), nonnull, sql.Identifier(parent_schema, parent), equal)
        if connection.execute(query).fetchone():
            raise ValueError(f"Foreign-key integrity failed in table {table}; import rolled back.")
        if match == "f":
            anynull = sql.SQL(" OR ").join(sql.SQL("c.{} IS NULL").format(sql.Identifier(a)) for a in children)
            anynonnull = sql.SQL(" OR ").join(sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(a)) for a in children)
            query = sql.SQL("SELECT 1 FROM {} c WHERE ({}) AND ({}) LIMIT 1").format(sql.Identifier(schema, table), anynull, anynonnull)
            if connection.execute(query).fetchone():
                raise ValueError(f"Composite foreign-key integrity failed in table {table}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--target-secret-file", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--verify-current-schema", action="store_true", help="For a fully current source: prove every schema object against a fresh database, preserving and reporting historical checksum differences.")
    args = parser.parse_args()
    if not args.source.is_file() or args.snapshot.exists() or args.report.exists():
        raise ValueError("Source must exist, and snapshot/report must be new paths.")
    target = dotenv_values(args.target_secret_file).get("METAHUB_DATABASE_URL", "")
    if not target.startswith(("postgresql:", "postgres:")):
        raise ValueError("Explicit PostgreSQL destination is required.")
    original = sqlite3.connect(args.source.resolve().as_uri() + "?mode=ro", uri=True)
    snapshot = sqlite3.connect(args.snapshot)
    try:
        original.backup(snapshot)
    finally:
        original.close()
        snapshot.close()
    # Upgrade ONLY this isolated copy. Production SQLite and WAL files are untouched.
    schema_proof = None
    if args.verify_current_schema:
        schema_proof = verify_current_sqlite_schema(args.snapshot)
    else:
        from metahub_shared.db import apply_migrations
        apply_migrations("sqlite:///" + args.snapshot.resolve().as_posix())
    source = sqlite3.connect(args.snapshot.resolve().as_uri() + "?mode=ro", uri=True)
    if source.execute("PRAGMA quick_check").fetchone()[0] != "ok" or source.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("SQLite copy failed integrity checks; destination was not modified.")
    names = sorted(row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'") if row[0] not in EXCLUDED)
    records = []
    with psycopg.connect(target, autocommit=False) as destination:
        # Holds an exclusive import lease until successful commit or rollback.
        destination.execute("SELECT pg_advisory_xact_lock(1296389185, 20261014)")
        if not destination.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]:
            raise ValueError("Offline import requires the dedicated administrator, not the application role.")
        for name in ("global_accounts", "tenants", "virtual_keys", "usage_ledger"):
            if destination.execute("SELECT to_regclass(%s)", ("public." + name,)).fetchone()[0] and destination.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier("public", name))).fetchone():
                raise ValueError("Destination contains business data; refusing to replace it.")
        actual = {row[0] for row in destination.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        if set(names) - actual:
            raise ValueError("Destination schema is missing SQLite tables. Apply native migrations first.")
        destination.execute("SET LOCAL session_replication_role=replica")
        destination.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY").format(sql.SQL(",").join(sql.Identifier("public", name) for name in names)))
        for name in names:
            # table_info omits SQLite's generated columns. Include their values
            # in the copy/digest even when the PostgreSQL baseline stores them.
            expected_columns = {row[0] for row in destination.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (name,))}
            columns = source_columns(source, name, expected_columns)
            quoted = ','.join('"' + column.replace('"', '""') + '"' for column in columns)
            rows = source.execute('SELECT ' + quoted + ' FROM "' + name.replace('"', '""') + '"').fetchall()
            expected = digest(rows)
            copy = sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier("public", name), sql.SQL(",").join(map(sql.Identifier, columns)))
            with destination.cursor().copy(copy) as writer:
                for row in rows:
                    writer.write_row(row)
            observed = digest(destination.execute(sql.SQL("SELECT {} FROM {}").format(sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier("public", name))).fetchall())
            if observed != expected:
                raise ValueError(f"Row digest mismatch in {name}; import rolled back.")
            records.append({"table": name, "rows": observed[0], "sha256": observed[1]})
            if len(records) % 100 == 0:
                print(json.dumps({"verified_tables": len(records)}), flush=True)
        # Sequence defaults are independent of inserted explicit IDs.
        for table, column in destination.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND (is_identity='YES' OR column_default LIKE 'nextval(%')"):
            sequence = destination.execute("SELECT pg_get_serial_sequence(%s,%s)", ('"public"."' + table + '"', column)).fetchone()[0]
            if sequence:
                maximum = destination.execute(sql.SQL("SELECT max({}) FROM {}").format(sql.Identifier(column), sql.Identifier("public", table))).fetchone()[0]
                destination.execute("SELECT setval(%s::regclass,%s,%s)", (sequence, maximum or 1, maximum is not None))
        destination.execute("SET LOCAL session_replication_role=origin")
        verify_foreign_keys(destination)
    source.close()
    report = {"status": "verified", "source_modified": False, "tables": records, "total_rows": sum(record["rows"] for record in records), "encryption": "ciphertext copied unchanged; original encryption roots must be retained or rewrapped separately"}
    report["source_schema_proof"] = schema_proof
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    args.report.chmod(0o600)
    print(json.dumps({"status": "verified", "tables": len(records), "rows": report["total_rows"], "source_modified": False}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # SQL errors can include failing rows, so never print their raw message.
        print(json.dumps({"status": "error", "exception_type": type(error).__name__, "phase": "sqlite_import"}), file=sys.stderr)
        raise SystemExit(1) from None
