"""Read bounded, data-derived recovery fingerprints without projecting row values."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_PAGE_SIZE = 256
_IDENTIFIER = r"^[a-z][a-z0-9_]{0,127}$"
_OWNER_TABLES = {"workspaces", "tenants", "workspace_tenant_bindings"}
_CONFIG_TABLES = {
    "config_snapshots",
    "config_snapshot_heads",
    "config_deltas",
    "config_acks",
    "policy_snapshots",
    "policy_snapshot_items",
    "policy_snapshot_heads",
    "workspace_gateway_placements",
}


class TableFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    table_name: str = Field(pattern=_IDENTIFIER)
    component_code: Literal["database", "ownership", "ledger", "outbox", "config"]
    row_count: int = Field(ge=0)
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _value(value: object) -> object:
    # Explicit type tags distinguish blobs/text and integer/float/null values.
    if value is None:
        return ["null"]
    if type(value) is str:
        return ["text", value]
    if type(value) in {int, bool}:
        return ["integer", str(int(value))]
    if type(value) is float and math.isfinite(value):
        return ["real", value.hex()]
    if isinstance(value, (bytes, memoryview)):
        return ["blob", base64.b64encode(value).decode("ascii")]
    raise ValueError("Unsupported recovery value type.")


def _component(name: str) -> str | None:
    if name == "schema_migrations":
        return "database"
    if name in _OWNER_TABLES:
        return "ownership"
    if "outbox" in name:
        return "outbox"
    if "ledger" in name:
        return "ledger"
    if name in _CONFIG_TABLES:
        return "config"
    return None


def fingerprint_database(connection, *, backend: str) -> tuple[TableFingerprint, ...]:
    """Caller owns a stable read transaction or an isolated, immutable snapshot."""
    if backend == "sqlite":
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
    elif backend == "postgresql":
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'"
            ).fetchall()
        ]
    else:
        raise ValueError("Unsupported recovery backend.")
    result = []
    for name in sorted(names):
        component = _component(name)
        if component is None:
            continue
        # Validate catalog identifiers before interpolating them into SQL.
        TableFingerprint(
            table_name=name,
            component_code=component,
            row_count=0,
            schema_sha256="0" * 64,
            content_sha256="0" * 64,
        )
        columns, primary = _columns(connection, name, backend)
        if not primary:
            raise ValueError("Recovery tables require a primary key.")
        schema_hash = hashlib.sha256(_canonical([name, columns, primary])).hexdigest()
        digest = hashlib.sha256(_canonical(["metahub-recovery-rows-v1", schema_hash]))
        column_names = [column[0] for column in columns]
        quoted_columns = ",".join(f'"{column}"' for column in column_names)
        keys = ",".join(f'"{column}"' for column in primary)
        qualified = f'"{name}"' if backend == "sqlite" else f'public."{name}"'
        count = 0
        last = None
        while True:
            parameters = () if last is None else last
            where = (
                "" if last is None else (f" WHERE ({keys}) > ({','.join('?' for _ in primary)})")
            )
            cursor = connection.execute(
                f"SELECT {quoted_columns} FROM {qualified}{where} ORDER BY {keys} LIMIT ?",
                (*parameters, _PAGE_SIZE),
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
            if not rows:
                break
            for row in rows:
                values = tuple(row)
                key = tuple(values[column_names.index(column)] for column in primary)
                if any(value is None for value in key):
                    raise ValueError("Recovery primary keys cannot contain nulls.")
                encoded = _canonical([_value(value) for value in values])
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                count += 1
            last = key
        result.append(
            TableFingerprint(
                table_name=name,
                component_code=component,
                row_count=count,
                schema_sha256=schema_hash,
                content_sha256=digest.hexdigest(),
            )
        )
    if not {"schema_migrations", *_OWNER_TABLES}.issubset(item.table_name for item in result):
        raise ValueError("Recovery ownership schema is missing.")
    return tuple(result)


def _columns(connection, name: str, backend: str) -> tuple[list, list[str]]:
    if backend == "sqlite":
        rows = connection.execute(f'PRAGMA table_info("{name}")').fetchall()
        columns = [(str(row[1]), str(row[2]), int(row[3])) for row in rows]
        primary = [str(row[1]) for row in sorted(rows, key=lambda row: int(row[5])) if row[5]]
    else:
        rows = connection.execute(
            """SELECT a.attname,pg_catalog.format_type(a.atttypid,a.atttypmod),
                      a.attnotnull,coalesce(array_position(k.conkey,a.attnum),0)
               FROM pg_catalog.pg_attribute a
               JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
               JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
               LEFT JOIN pg_catalog.pg_constraint k ON k.conrelid=c.oid AND k.contype='p'
               WHERE n.nspname='public' AND c.relname=? AND a.attnum>0 AND NOT a.attisdropped
               ORDER BY a.attnum""",
            (name,),
        ).fetchall()
        columns = [(str(row[0]), str(row[1]), int(row[2])) for row in rows]
        primary = [str(row[0]) for row in sorted(rows, key=lambda row: int(row[3])) if row[3]]
    # Column identifiers are catalog-derived too, but unsupported names must fail closed.
    import re

    if not columns or any(re.fullmatch(_IDENTIFIER, column[0]) is None for column in columns):
        raise ValueError("Unsupported recovery columns.")
    return columns, primary
