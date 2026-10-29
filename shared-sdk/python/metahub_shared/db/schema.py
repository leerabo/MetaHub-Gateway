"""Backend-aware schema inspection without changing transaction ownership."""

from __future__ import annotations

import re
import sqlite3


def _table_name(table: str) -> str:
    if not isinstance(table, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) is None:
        raise ValueError("Invalid schema table name.")
    return table


def table_exists(connection, table: str) -> bool:
    table = _table_name(table)
    if getattr(connection, "backend", "sqlite") == "postgresql":
        row = connection.execute(
            "SELECT 1 FROM pg_catalog.pg_class "
            "WHERE relnamespace=current_schema()::regnamespace AND relkind IN ('r','p') "
            "AND relname=?",
            (table,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT 1 FROM pragma_table_list(?) WHERE schema='main' "
            "AND type IN('table','virtual','shadow') AND name=?", (table, table),
        ).fetchone()
    return row is not None


def table_columns(connection, table: str) -> tuple[str, ...]:
    table = _table_name(table)
    if getattr(connection, "backend", "sqlite") == "postgresql":
        rows = connection.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=? ORDER BY ordinal_position",
            (table,),
        ).fetchall()
    else:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(str(row["name"]) for row in rows)


def existing_table_names(connection, tables) -> frozenset[str]:
    """Find requested main-schema tables in the caller's current snapshot."""
    names = tuple(sorted({_table_name(table) for table in tables}))
    if not names:
        return frozenset()
    placeholders = ",".join("?" for _ in names)
    if getattr(connection, "backend", "sqlite") == "postgresql":
        query = (
            "SELECT relname FROM pg_catalog.pg_class "
            "WHERE relnamespace=current_schema()::regnamespace AND relkind IN ('r','p') "
            f"AND relname IN ({placeholders})"
        )
    else:
        # Named PRAGMA lookups use SQLite's parsed schema instead of scanning every
        # trigger/index definition. Keep one statement and no cross-snapshot cache.
        values = ",".join("(?)" for _ in names)
        query = (
            f"WITH requested(name) AS (VALUES {values}) "
            "SELECT requested.name FROM requested "
            "JOIN pragma_table_list(requested.name) listed "
            "ON listed.schema='main' AND listed.name=requested.name "
            "AND listed.type IN ('table','virtual','shadow') "
            "AND listed.name!='sqlite_schema'"
        )
    return frozenset(str(row[0]) for row in connection.execute(query, names).fetchall())


def tables_exist(connection, tables) -> bool:
    """Check one required table set in the caller's snapshot without N round trips."""
    names = frozenset(_table_name(table) for table in tables)
    return existing_table_names(connection, names) == names


def foreign_key_violation_count(connection) -> int:
    if getattr(connection, "backend", "sqlite") != "postgresql":
        return len(connection.execute("PRAGMA foreign_key_check").fetchall())

    from psycopg import sql

    if connection.execute("SHOW session_replication_role").fetchone()[0] == "replica":
        raise sqlite3.DatabaseError("Foreign key enforcement is unavailable.")
    constraints = connection.execute(
        """SELECT child_ns.nspname AS child_schema,child.relname AS child_table,
                  parent_ns.nspname AS parent_schema,parent.relname AS parent_table,
                  fk.confmatchtype,fk.convalidated,
                  EXISTS(SELECT 1 FROM pg_trigger trigger
                         WHERE trigger.tgconstraint=fk.oid
                           AND trigger.tgenabled NOT IN('O','A')) AS disabled,
                  ARRAY(SELECT attribute.attname
                        FROM unnest(fk.conkey) WITH ORDINALITY key(attnum,ordinal)
                        JOIN pg_attribute attribute
                          ON attribute.attrelid=fk.conrelid AND attribute.attnum=key.attnum
                        ORDER BY key.ordinal) AS child_columns,
                  ARRAY(SELECT attribute.attname
                        FROM unnest(fk.confkey) WITH ORDINALITY key(attnum,ordinal)
                        JOIN pg_attribute attribute
                          ON attribute.attrelid=fk.confrelid AND attribute.attnum=key.attnum
                        ORDER BY key.ordinal) AS parent_columns
           FROM pg_constraint fk
           JOIN pg_class child ON child.oid=fk.conrelid
           JOIN pg_namespace child_ns ON child_ns.oid=child.relnamespace
           JOIN pg_class parent ON parent.oid=fk.confrelid
           JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace
           WHERE fk.contype='f' AND child_ns.nspname=current_schema()
           ORDER BY fk.oid"""
    ).fetchall()
    queries = []
    for constraint in constraints:
        if (
            not constraint["convalidated"] or constraint["disabled"]
            or constraint["confmatchtype"] not in {"s", "f"}
        ):
            raise sqlite3.DatabaseError("Foreign key enforcement is unavailable.")
        child_columns = constraint["child_columns"]
        parent_columns = constraint["parent_columns"]
        nonnull = [sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(name))
                   for name in child_columns]
        comparisons = [sql.SQL("p.{}=c.{}").format(sql.Identifier(parent), sql.Identifier(child))
                       for child, parent in zip(child_columns, parent_columns, strict=True)]
        missing = sql.SQL(
            "({nonnull}) AND NOT EXISTS(SELECT 1 FROM {parent} p WHERE {matches})"
        ).format(
            nonnull=sql.SQL(" AND ").join(nonnull),
            parent=sql.Identifier(constraint["parent_schema"], constraint["parent_table"]),
            matches=sql.SQL(" AND ").join(comparisons),
        )
        if constraint["confmatchtype"] == "f":
            nulls = [sql.SQL("c.{} IS NULL").format(sql.Identifier(name)) for name in child_columns]
            missing = sql.SQL("({missing}) OR (({nonnull}) AND ({nulls}))").format(
                missing=missing, nonnull=sql.SQL(" OR ").join(nonnull),
                nulls=sql.SQL(" OR ").join(nulls),
            )
        queries.append(
            sql.SQL("SELECT count(*) AS violations FROM {child} c WHERE {missing}").format(
                child=sql.Identifier(constraint["child_schema"], constraint["child_table"]),
                missing=missing,
            )
        )
    # Bound planning work while avoiding a separate network round trip for every FK.
    count = 0
    for offset in range(0, len(queries), 64):
        statement = sql.SQL(" UNION ALL ").join(queries[offset:offset + 64]).as_string()
        count += sum(int(row[0]) for row in connection.execute(statement).fetchall())
    return count
