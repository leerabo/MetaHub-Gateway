"""Execute every generated PostgreSQL baseline trigger against an isolated row."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

EXPECTED_REJECTION_TRIGGERS = 2421
EXPECTED_ACTION_TRIGGERS = 65
UNEXPECTED_SQLSTATE_CLASSES = {"0A", "2F", "38", "39", "3B", "42", "XX"}
IF_LINE = re.compile(r"(?im)^(?P<indent>\s*)IF\b.*\bTHEN\s*$")
DML_LINE = re.compile(r"(?im)^\s*(?:INSERT\s+INTO|UPDATE\s+.+?\s+SET|DELETE\s+FROM)\b")
TRIGGER_DEFINITION = re.compile(
    r"(?is)^CREATE\s+TRIGGER\s+.+?\s+"
    r"(?P<timing>BEFORE|AFTER)\s+"
    r"(?P<event>INSERT|UPDATE|DELETE)"
    r"(?:\s+OF\s+(?P<columns>.+?))?\s+ON\s+"
)


@dataclass(frozen=True)
class TriggerProbe:
    name: str
    table: str
    function: str
    source: str
    definition: str
    first_column: str

    @property
    def action(self) -> bool:
        return DML_LINE.search(self.source) is not None

    @property
    def ignores_row(self) -> bool:
        return "RETURN NULL;" in self.source


@dataclass(frozen=True)
class ProbeFailure:
    trigger: str
    phase: str
    sqlstate: str | None
    detail: str | None


def error_detail(error: psycopg.Error) -> str | None:
    details = [error.diag.message_primary, error.diag.context]
    if error.diag.internal_query and error.diag.internal_position:
        position = int(error.diag.internal_position) - 1
        start = max(0, position - 120)
        end = min(len(error.diag.internal_query), position + 120)
        details.append(error.diag.internal_query[start:end])
    rendered = " | ".join(item.replace("\n", " ") for item in details if item)
    return rendered or None


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url-env",
        default="METAHUB_TEST_POSTGRESQL_URL",
        help="Environment variable holding the disposable PostgreSQL URL.",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument(
        "--failures-from",
        type=Path,
        help="Re-run only trigger names recorded as failures in a prior probe JSON.",
    )
    return parser.parse_args()


def load_triggers(connection: psycopg.Connection[Any]) -> list[TriggerProbe]:
    rows = connection.execute(
        """
        SELECT
          trigger.tgname,
          relation.relname,
          function.proname,
          function.prosrc,
          pg_get_triggerdef(trigger.oid,true),
          first_column.attname
        FROM pg_trigger trigger
        JOIN pg_class relation ON relation.oid=trigger.tgrelid
        JOIN pg_namespace relation_namespace
          ON relation_namespace.oid=relation.relnamespace
        JOIN pg_proc function ON function.oid=trigger.tgfoid
        JOIN LATERAL(
          SELECT attribute.attname
          FROM pg_attribute attribute
          WHERE attribute.attrelid=relation.oid AND attribute.attnum>0
            AND NOT attribute.attisdropped
          ORDER BY attribute.attnum
          LIMIT 1
        ) first_column ON TRUE
        WHERE relation_namespace.nspname='public' AND NOT trigger.tgisinternal
        ORDER BY relation.relname,trigger.tgname
        """
    ).fetchall()
    connection.rollback()
    return [TriggerProbe(*row) for row in rows]


def trigger_shape(probe: TriggerProbe) -> tuple[str, str, list[str]]:
    match = TRIGGER_DEFINITION.match(probe.definition)
    if match is None:
        raise ValueError(f"Unrecognized trigger definition: {probe.name}")
    columns = []
    if match.group("columns"):
        columns = [
            column.strip().strip('"').replace('""', '"')
            for column in match.group("columns").split(",")
        ]
    return match.group("timing").upper(), match.group("event").upper(), columns


def forced_source(source: str) -> str:
    return IF_LINE.sub(lambda match: f"{match.group('indent')}IF TRUE THEN", source)


def run_trigger(
    connection: psycopg.Connection[Any],
    probe: TriggerProbe,
    *,
    force_conditions: bool,
) -> tuple[str | None, str | None]:
    timing, event, update_columns = trigger_shape(probe)
    connection.execute("SET LOCAL statement_timeout='5s'")
    connection.execute(
        sql.SQL("CREATE TEMP TABLE metahub_trigger_probe AS TABLE {} WITH NO DATA").format(
            sql.Identifier("public", probe.table)
        )
    )
    function_name = sql.Identifier("public", probe.function)
    if force_conditions:
        connection.execute(
            sql.SQL(
                "CREATE FUNCTION pg_temp.metahub_trigger_probe_fn() RETURNS trigger "
                "LANGUAGE plpgsql AS {}"
            ).format(sql.Literal(forced_source(probe.source)))
        )
        function_name = sql.Identifier("pg_temp", "metahub_trigger_probe_fn")
    if event in {"UPDATE", "DELETE"}:
        connection.execute("INSERT INTO metahub_trigger_probe DEFAULT VALUES")
    update_clause = sql.SQL("")
    if update_columns:
        update_clause = sql.SQL(" OF {} ").format(
            sql.SQL(",").join(sql.Identifier(column) for column in update_columns)
        )
    trigger_sql = sql.SQL(
        "CREATE TRIGGER metahub_trigger_probe {} {}{} ON metahub_trigger_probe "
        "FOR EACH ROW EXECUTE FUNCTION {}()"
    ).format(
        sql.SQL(timing),
        sql.SQL(event),
        update_clause,
        function_name,
    )
    connection.execute(trigger_sql)
    update_column = update_columns[0] if update_columns else probe.first_column
    try:
        if event == "INSERT":
            connection.execute("INSERT INTO metahub_trigger_probe DEFAULT VALUES")
        elif event == "DELETE":
            connection.execute("DELETE FROM metahub_trigger_probe")
        else:
            connection.execute(
                sql.SQL("UPDATE metahub_trigger_probe SET {}={}").format(
                    sql.Identifier(update_column),
                    sql.Identifier(update_column),
                )
            )
    except psycopg.Error as exc:
        return exc.sqlstate, error_detail(exc)
    return None, None


def phase_failure(
    probe: TriggerProbe,
    phase: str,
    sqlstate: str | None,
    detail: str | None,
) -> ProbeFailure | None:
    if phase == "forced_rejection":
        if sqlstate != "23514":
            return ProbeFailure(probe.name, phase, sqlstate, detail)
        return None
    if phase == "forced_ignore":
        if sqlstate is not None:
            return ProbeFailure(probe.name, phase, sqlstate, detail)
        return None
    if sqlstate is not None and sqlstate[:2] in UNEXPECTED_SQLSTATE_CLASSES:
        return ProbeFailure(probe.name, phase, sqlstate, detail)
    return None


def main() -> int:
    arguments = parse_arguments()
    database_url = os.getenv(arguments.database_url_env)
    if not database_url:
        raise SystemExit(f"{arguments.database_url_env} is required")
    started = time.perf_counter()
    failures: list[ProbeFailure] = []
    outcomes: dict[str, dict[str, int]] = {
        "neutral": {},
        "forced_rejection": {},
        "forced_ignore": {},
        "forced_action": {},
    }
    with psycopg.connect(database_url, autocommit=False) as connection:
        probes = load_triggers(connection)
        action_count = sum(probe.action for probe in probes)
        rejection_count = len(probes) - action_count
        if (
            rejection_count != EXPECTED_REJECTION_TRIGGERS
            or action_count != EXPECTED_ACTION_TRIGGERS
        ):
            raise RuntimeError(
                f"trigger classification changed: rejection={rejection_count} "
                f"action={action_count}"
            )
        if arguments.failures_from is not None:
            previous = json.loads(arguments.failures_from.read_text(encoding="utf-8"))
            failed_names = {item["trigger"] for item in previous["failures"]}
            probes = [probe for probe in probes if probe.name in failed_names]
        for probe in probes:
            forced_phase = (
                "forced_action"
                if probe.action
                else "forced_ignore"
                if probe.ignores_row
                else "forced_rejection"
            )
            phases = ["neutral", forced_phase]
            for phase in phases:
                try:
                    sqlstate, detail = run_trigger(
                        connection,
                        probe,
                        force_conditions=phase != "neutral",
                    )
                except psycopg.Error as exc:
                    sqlstate = exc.sqlstate
                    detail = error_detail(exc)
                finally:
                    connection.rollback()
                outcome = sqlstate or "accepted"
                outcomes[phase][outcome] = outcomes[phase].get(outcome, 0) + 1
                failure = phase_failure(probe, phase, sqlstate, detail)
                if failure is not None:
                    failures.append(failure)

    payload = {
        "schema": "metahub.postgresql-trigger-probe.v1",
        "status": "passed" if not failures else "failed",
        "trigger_count": len(probes),
        "rejection_trigger_count": rejection_count,
        "action_trigger_count": action_count,
        "phases": outcomes,
        "failure_count": len(failures),
        "failures": [asdict(failure) for failure in failures[:100]],
        "duration_seconds": round(time.perf_counter() - started, 3),
    }
    rendered = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
    if arguments.json_output is not None:
        arguments.json_output.parent.mkdir(parents=True, exist_ok=True)
        arguments.json_output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
