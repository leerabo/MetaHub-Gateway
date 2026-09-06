from __future__ import annotations

# ruff: noqa: E501 -- SQL fragments stay one generated statement per source string.
import argparse
import hashlib
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPOSITORY_ROOT / "infra" / "migrations"
DEFAULT_OUTPUT = MIGRATIONS_DIR / "postgresql" / "0139_postgresql_baseline.sql"
BASELINE_VERSION = "0139"
GENERATOR_REVISION = 4
CANONICAL_SEED_DATETIME = "2026-09-01 00:00:00"
CANONICAL_SEED_TIMESTAMP = "2026-09-01T00:00:00Z"
CANONICAL_SEED_TIMESTAMP_MILLIS = "2026-09-01T00:00:00.000Z"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the reviewed PostgreSQL baseline from SQLite migrations through 0139."
    )
    parser.add_argument(
        "--source-sqlite",
        type=Path,
        help="Use an existing canonical SQLite database instead of rebuilding it.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def build_canonical_source(workspace: Path) -> Path:
    migration_subset = workspace / "migrations"
    migration_subset.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and source.name[:4] <= BASELINE_VERSION:
            shutil.copy2(source, migration_subset / source.name)
    sys.path.insert(0, str(REPOSITORY_ROOT / "shared-sdk" / "python"))
    from metahub_shared.db import apply_migrations

    database = workspace / "canonical.sqlite3"
    result = apply_migrations(
        f"sqlite:///{database.as_posix()}",
        migrations_dir=migration_subset,
    )
    if not result.applied or result.applied[-1] != BASELINE_VERSION:
        raise RuntimeError("Canonical SQLite source did not reach migration 0139")
    return database


def migration_manifest_sha256(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    rows = connection.execute(
        "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    for row in rows:
        digest.update(f"{row['version']}\0{row['name']}\0{row['checksum']}\n".encode())
    return digest.hexdigest()


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def quote_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def postgres_type(declared: str) -> str:
    normalized = declared.strip().upper()
    if "INT" in normalized:
        return "BIGINT"
    if any(token in normalized for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in normalized or not normalized:
        return "BYTEA"
    if any(token in normalized for token in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE PRECISION"
    return "NUMERIC"


def postgres_default(value: str | None, target_type: str) -> str | None:
    if value is None:
        return None
    result = value.strip()
    if len(result) >= 2 and result[0] == result[-1] == '"':
        result = quote_text(result[1:-1].replace('""', '"'))
    result = re.sub(
        r"(?i)datetime\s*\(\s*'now'\s*\)",
        "metahub_utc_now()",
        result,
    )
    result = re.sub(
        r"(?i)strftime\s*\(\s*'[^']+'\s*,\s*'now'\s*\)",
        "metahub_utc_now()",
        result,
    )
    result = result.replace("||", "||")
    if target_type == "BYTEA" and result.upper() in {"''", "X''"}:
        return "decode('', 'hex')"
    return result


def literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"decode('{value.hex()}', 'hex')"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return quote_text(str(value))


def migration_execution_window(
    connection: sqlite3.Connection,
) -> tuple[datetime, datetime]:
    timestamps = [
        datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).replace(tzinfo=None)
        for row in connection.execute(
            "SELECT applied_at FROM schema_migrations WHERE status='applied'"
        ).fetchall()
    ]
    if len(timestamps) != 139:
        raise RuntimeError("Canonical SQLite migration history is incomplete")
    margin = timedelta(minutes=1)
    return min(timestamps) - margin, max(timestamps) + margin


def canonical_seed_value(
    table_name: str,
    column_name: str,
    value: object,
    execution_window: tuple[datetime, datetime],
) -> object:
    """Remove migration runtime timing from the reviewed baseline artifact."""

    if table_name == "schema_migrations" and column_name == "execution_time_ms":
        return 0
    if not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except ValueError:
        return value
    if not execution_window[0] <= parsed <= execution_window[1]:
        return value
    if "T" not in value:
        return CANONICAL_SEED_DATETIME
    if "." in value:
        return CANONICAL_SEED_TIMESTAMP_MILLIS
    return CANONICAL_SEED_TIMESTAMP


def stable_name(prefix: str, *parts: str) -> str:
    raw = "_".join((prefix, *parts))
    if len(raw.encode("utf-8")) <= 63:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return raw[: 50 - len(prefix)] + "_" + digest


def balanced_checks(sql: str) -> list[str]:
    checks: list[str] = []
    cursor = 0
    while True:
        match = re.search(r"(?i)\bCHECK\s*\(", sql[cursor:])
        if match is None:
            return checks
        start = cursor + match.end()
        depth = 1
        quote: str | None = None
        index = start
        while index < len(sql) and depth:
            character = sql[index]
            if quote is not None:
                if character == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
            elif character in {"'", '"'}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
            index += 1
        if depth:
            raise ValueError("Unbalanced SQLite CHECK expression")
        checks.append(sql[start : index - 1].strip())
        cursor = index


NUMERIC_JSON_PATHS = {
    "$.active_tool_count",
    "$.adjustment_count",
    "$.chat",
    "$.checked_policy_count",
    "$.configuration_schema_version",
    "$.destination_count",
    "$.feedback_version",
    "$.item_count",
    "$.manual_quality_bps",
    "$.previous_manual_quality_bps",
    "$.priority",
    "$.permission_count",
    "$.project_count",
    "$.quality_cost_config.cost_weight_bps",
    "$.quality_cost_config.latency_weight_bps",
    "$.quality_cost_config.minimum_sample_count",
    "$.quality_cost_config.quality_floor_bps",
    "$.quality_cost_config.quality_weight_bps",
    "$.requires_step_up",
    "$.role_grant_count",
    "$.rule_version",
    "$.routing_policy_version",
    "$.sample_count",
    "$.statistics_version",
    "$.template_revision",
    "$.tool_count",
    "$.required_permission_count",
    "$.user_grant_count",
    "$.version",
    "$.waiting_job_resume_count",
}
NUMERIC_COLUMN_NAMES: set[str] = set()
NON_NUMERIC_COLUMN_NAMES: set[str] = set()


def cast_numeric_json_extracts(expression: str) -> str:
    result = expression
    guarded_paths: set[tuple[str, str]] = set()
    for match in re.finditer(
        r"(?i)json_type\s*\(\s*([^,()]+?)\s*,\s*'([^']+)'\s*\)\s*"
        r"(?:=\s*'integer'|IN\s*\([^)]*'integer'[^)]*\))",
        result,
    ):
        guarded_paths.add((match.group(1).strip(), match.group(2)))
    for document, path in guarded_paths:
        result = re.sub(
            rf"(?i)(json_extract\s*\(\s*{re.escape(document)}\s*,\s*'{re.escape(path)}'\s*\))",
            r"(\1)::NUMERIC",
            result,
        )
    for path in sorted(NUMERIC_JSON_PATHS, key=len, reverse=True):
        escaped_path = re.escape(path)
        result = re.sub(
            rf"(?i)(json_extract\s*\([^,]+,\s*'{escaped_path}'\s*\))",
            r"(\1)::NUMERIC",
            result,
        )
    result = re.sub(
        r"(?i)(json_extract\s*\([^,]+,\s*'([^']+)'\s*\))",
        lambda match: (
            f"({match.group(1)})::NUMERIC"
            if match.group(2).rsplit(".", 1)[-1].split("[", 1)[0]
            in NUMERIC_COLUMN_NAMES
            and "::NUMERIC" not in match.group(0).upper()
            else match.group(1)
        ),
        result,
    )
    result = re.sub(
        r"(?i)(json_extract\s*\([^,]+,\s*'[^']+'\s*\))"
        r"(?=\s*(?:=|<>|<=|>=|<|>)\s*-?[0-9]+\b)",
        r"(\1)::NUMERIC",
        result,
    )
    duplicate_cast = re.compile(
        r"(?i)\(\((json_extract\s*\([^,]+,\s*'[^']+'\s*\))\)::NUMERIC\)::NUMERIC"
    )
    while duplicate_cast.search(result):
        result = duplicate_cast.sub(r"(\1)::NUMERIC", result)
    return result


def normalize_sqlite_boolean_comparisons(expression: str) -> str:
    function_call = r"json_valid\s*\([^()]*\)"
    result = re.sub(
        rf"(?i)({function_call})\s*(?:=|==)\s*1\b",
        r"\1",
        expression,
    )
    result = re.sub(
        rf"(?i)({function_call})\s*(?:=|==)\s*0\b",
        r"NOT \1",
        result,
    )
    result = re.sub(
        rf"(?i)1\s*(?:=|==)\s*({function_call})",
        r"\1",
        result,
    )
    result = re.sub(
        rf"(?i)0\s*(?:=|==)\s*({function_call})",
        r"NOT \1",
        result,
    )
    # A SQLite CASE used as a predicate may combine boolean branches with its
    # conventional integer fallback. PostgreSQL requires a single boolean type.
    while re.search(r"(?i)ELSE\s+1\s+END(?=\s*(?:ELSE\s+1\s+END)?\s*$)", result):
        result = re.sub(
            r"(?i)ELSE\s+1\s+END(?=\s*(?:ELSE\s+1\s+END)?\s*$)",
            "ELSE TRUE END",
            result,
            count=1,
        )
    integer_case = re.fullmatch(
        r"(?is)(.*?\b(?:AND|OR)\s+)(CASE\b.*\bELSE\s+0\s+END)",
        result,
    )
    if integer_case is not None:
        result = f"{integer_case.group(1)}(({integer_case.group(2)})<>0)"
    return result


def replace_boolean_coalesce_defaults(expression: str) -> str:
    replacements: list[tuple[int, int]] = []
    for match in re.finditer(r"(?i)\bCOALESCE\s*\(", expression):
        depth = 1
        quote: str | None = None
        last_comma: int | None = None
        index = match.end()
        while index < len(expression) and depth:
            character = expression[index]
            if quote is not None:
                if character == quote:
                    if index + 1 < len(expression) and expression[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
            elif character in {"'", '"'}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
            elif character == "," and depth == 1:
                last_comma = index
            index += 1
        if depth == 0 and last_comma is not None:
            default = expression[last_comma + 1 : index - 1].strip()
            if default == "0":
                start = last_comma + 1
                while start < index - 1 and expression[start].isspace():
                    start += 1
                end = index - 1
                while end > start and expression[end - 1].isspace():
                    end -= 1
                replacements.append((start, end))
    result = expression
    for start, end in reversed(replacements):
        result = result[:start] + "FALSE" + result[end:]
    return result


def strip_sql_comments(sql: str) -> str:
    output: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            output.append(character)
            if character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
            output.append(character)
        elif sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            if newline < 0:
                break
            output.append(" ")
            index = newline
        elif sql.startswith("/*", index):
            closing = sql.find("*/", index + 2)
            if closing < 0:
                raise ValueError("Unclosed SQL block comment")
            output.append(" ")
            index = closing + 1
        else:
            output.append(character)
        index += 1
    return "".join(output)


def postgres_expression(expression: str) -> str:
    result = strip_sql_comments(expression)
    result = re.sub(
        r"(?i)\blength\s*\(\s*CAST\s*\((.*?)\s+AS\s+BLOB\s*\)\s*\)",
        r"octet_length(convert_to(\1,'UTF8'))",
        result,
    )
    result = re.sub(r"(?i)\bCAST\s*\((.*?)\s+AS\s+BLOB\s*\)", r"CAST(\1 AS BYTEA)", result)
    result = re.sub(r"(?i)\bCOLLATE\s+NOCASE\b", "", result)
    result = re.sub(r"(?i)\bNOT\s+GLOB\b", "OPERATOR(public.!@@)", result)
    result = re.sub(r"(?i)\bGLOB\b", "OPERATOR(public.@@)", result)
    result = re.sub(r"(?i)\bIS\s+NOT\s+(?!NULL\b|TRUE\b|FALSE\b|UNKNOWN\b|DISTINCT\b)", "IS DISTINCT FROM ", result)
    result = re.sub(r"(?i)\bIS\s+(?!NOT\b|NULL\b|TRUE\b|FALSE\b|UNKNOWN\b|DISTINCT\b)", "IS NOT DISTINCT FROM ", result)
    result = re.sub(r"(?<![<>!])==(?![=])", "=", result)
    result = re.sub(r"(?i)\bmin\s*\(", "LEAST(", result)
    result = re.sub(r"(?i)\bmax\s*\(", "GREATEST(", result)
    result = re.sub(r"(?i)\bchar\s*\(", "chr(", result)
    # PostgreSQL text cannot store NUL, so SQLite's explicit NUL rejection is
    # redundant. Keep control-character ranges equivalent for storable text.
    result = re.sub(
        r"(?i)\binstr\s*\(\s*([^,()]+?)\s*,\s*chr\s*\(\s*0\s*\)\s*\)\s*=\s*0",
        "TRUE",
        result,
    )
    result = re.sub(
        r"(?i)chr\s*\(\s*0\s*\)\s*\|\|\s*'-'\s*\|\|\s*chr\s*\(\s*31\s*\)",
        "chr(1)||'-'||chr(31)",
        result,
    )
    result = re.sub(r"(?i)\bjson\s*\(", "metahub_json(", result)
    result = cast_numeric_json_extracts(result)
    result = normalize_sqlite_boolean_comparisons(result)
    result = replace_boolean_coalesce_defaults(result)
    result = re.sub(r"(?i)=\s*body_truncated\b", "=(body_truncated=1)", result)
    result = re.sub(r"\s+", " ", result).strip()
    result = result.replace(
        "display_name OPERATOR(public.!@@) '*['||chr(1)||'-'||chr(31)||']*'",
        "metahub_not_glob(display_name,'*['||chr(1)||'-'||chr(31)||']*')",
    )
    result = result.replace(
        r"filename OPERATOR(public.!@@) '*[/\\]*'",
        "(strpos(filename,'/')=0 AND strpos(filename,chr(92))=0)",
    )
    result = result.replace(
        r"component_name OPERATOR(public.!@@) '*[/\\:]*'",
        "(strpos(component_name,'/')=0 AND strpos(component_name,chr(92))=0 AND strpos(component_name,':')=0)",
    )
    return result


def matching_parenthesis(value: str, opening: int) -> int:
    depth = 1
    quote: str | None = None
    index = opening + 1
    while index < len(value):
        character = value[index]
        if quote is not None:
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError("Unbalanced SQL function call")


def top_level_comma(value: str) -> bool:
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            return True
        index += 1
    return False


def rename_multi_argument_function(sql: str, source: str, target: str) -> str:
    replacements: list[tuple[int, int]] = []
    for match in re.finditer(rf"(?i)\b{re.escape(source)}\s*\(", sql):
        opening = match.end() - 1
        closing = matching_parenthesis(sql, opening)
        if top_level_comma(sql[opening + 1 : closing]):
            replacements.append((match.start(), opening))
    result = sql
    for start, end in reversed(replacements):
        result = result[:start] + target + result[end:]
    return result


def wrap_json_builders(sql: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(sql):
        if sql[index] in {"'", '"'}:
            quote = sql[index]
            start = index
            index += 1
            while index < len(sql):
                if sql[index] == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            output.append(sql[start:index])
            continue
        match = re.match(r"(?i)(json_array|json_object)\s*\(", sql[index:])
        if match and (index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] == "_")):
            opening = index + match.end() - 1
            closing = matching_parenthesis(sql, opening)
            target = "jsonb_build_array" if match.group(1).lower() == "json_array" else "jsonb_build_object"
            arguments = wrap_json_builders(sql[opening + 1 : closing])
            output.append(f"({target}({arguments})::TEXT)")
            index = closing + 1
            continue
        output.append(sql[index])
        index += 1
    return "".join(output)


def split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    start = 0
    quote: str | None = None
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            if character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character == ";":
            statement = sql[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1
        index += 1
    terminal = sql[start:].strip()
    if terminal:
        statements.append(terminal)
    return statements


TRIGGER_RE = re.compile(
    r"(?is)^\s*CREATE\s+TRIGGER(?:\s+IF\s+NOT\s+EXISTS)?\s+(?P<name>\S+)\s+"
    r"(?P<timing>BEFORE|AFTER)\s+(?P<event>INSERT|UPDATE|DELETE)"
    r"(?:\s+OF\s+(?P<columns>.+?))?\s+ON\s+(?P<table>\S+)"
    r"(?:\s+FOR\s+EACH\s+ROW)?(?:\s+WHEN\s*(?P<when>.*?))?\s+"
    r"BEGIN\s+(?P<body>.*)\s+END\s*$"
)


def parse_trigger(sql: str) -> dict[str, str | None]:
    clean = strip_sql_comments(sql).strip()
    match = TRIGGER_RE.match(clean)
    if match is None:
        raise ValueError(f"Unsupported SQLite trigger header: {clean[:160]}")
    return match.groupdict()


def raise_condition(body: str) -> tuple[str, str] | None:
    normalized = body.strip().rstrip(";").strip()
    if len(split_sql_statements(normalized)) != 1:
        return None
    unconditional = re.fullmatch(
        r"(?is)SELECT\s+RAISE\s*\(\s*ABORT\s*,\s*'([^']*)'\s*\)", normalized
    )
    if unconditional:
        return "TRUE", unconditional.group(1)
    where = re.fullmatch(
        r"(?is)SELECT\s+RAISE\s*\(\s*ABORT\s*,\s*'([^']*)'\s*\)\s+WHERE\s+(.+)",
        normalized,
    )
    if where:
        return where.group(2), where.group(1)
    case = re.fullmatch(
        r"(?is)SELECT\s+CASE\s+WHEN\s+(.+?)\s+THEN\s+"
        r"RAISE\s*\(\s*ABORT\s*,\s*'([^']*)'\s*\)\s+END",
        normalized,
    )
    if case:
        return case.group(1), case.group(2)
    return None


def conditional_ignore_or_abort(body: str) -> tuple[str, str] | None:
    normalized = body.strip().rstrip(";").strip()
    match = re.fullmatch(
        r"(?is)SELECT\s+CASE\s+WHEN\s+(.+?)\s+THEN\s+"
        r"RAISE\s*\(\s*IGNORE\s*\)\s+ELSE\s+"
        r"RAISE\s*\(\s*ABORT\s*,\s*'([^']*)'\s*\)\s+END",
        normalized,
    )
    if match is None:
        return None
    return match.group(1), match.group(2)


def postgres_trigger_fragment(sql: str) -> str:
    result = strip_sql_comments(sql)
    result = re.sub(r"(?i)\b(FROM|JOIN)\s+users\s+user\b", r"\1 users user_row", result)
    result = re.sub(r"(?i)\buser\.", "user_row.", result)
    result = re.sub(
        r"(?i)\b(FROM|JOIN)\s+session_risk_freezes\s+freeze\b",
        r"\1 session_risk_freezes freeze_row",
        result,
    )
    result = re.sub(r"(?i)\bfreeze\.", "freeze_row.", result)
    result = re.sub(
        r"(?i)\b(FROM|JOIN)\s+access_token_grants\s+grant\b",
        r"\1 access_token_grants grant_row",
        result,
    )
    result = re.sub(r"(?i)\bgrant\.", "grant_row.", result)
    result = re.sub(
        r"(?i)\blength\s*\(\s*CAST\s*\((.*?)\s+AS\s+BLOB\s*\)\s*\)",
        r"octet_length(convert_to(\1,'UTF8'))",
        result,
    )
    result = re.sub(r"(?i)\bCAST\s*\((.*?)\s+AS\s+BLOB\s*\)", r"CAST(\1 AS BYTEA)", result)
    result = re.sub(r"(?i)\bCOLLATE\s+NOCASE\b", "", result)
    result = re.sub(r"(?i)\bNOT\s+GLOB\b", "OPERATOR(public.!@@)", result)
    result = re.sub(r"(?i)\bGLOB\b", "OPERATOR(public.@@)", result)
    result = re.sub(r"(?i)\bIS\s+NOT\s+(?!NULL\b|TRUE\b|FALSE\b|UNKNOWN\b|DISTINCT\b)", "IS DISTINCT FROM ", result)
    result = re.sub(r"(?i)\bIS\s+(?!NOT\b|NULL\b|TRUE\b|FALSE\b|UNKNOWN\b|DISTINCT\b)", "IS NOT DISTINCT FROM ", result)
    result = re.sub(r"(?<![<>!])==(?![=])", "=", result)
    result = rename_multi_argument_function(result, "min", "LEAST")
    result = rename_multi_argument_function(result, "max", "GREATEST")
    result = re.sub(r"(?i)\bchar\s*\(", "chr(", result)
    result = re.sub(
        r"(?i)\binstr\s*\(\s*([^,()]+?)\s*,\s*chr\s*\(\s*0\s*\)\s*\)\s*=\s*0",
        "TRUE",
        result,
    )
    result = re.sub(
        r"(?i)chr\s*\(\s*0\s*\)\s*\|\|\s*'-'\s*\|\|\s*chr\s*\(\s*31\s*\)",
        "chr(1)||'-'||chr(31)",
        result,
    )
    result = re.sub(r"(?i)\bjson\s*\(", "metahub_json(", result)
    result = re.sub(r"(?i)\bgroup_concat\s*\(", "string_agg(", result)
    result = re.sub(r"(?i)\bjson_quote\s*\(", "json_quote(", result)
    result = re.sub(r"(?i)\bNEW\.rowid\b", "NEW.ctid", result)
    result = re.sub(r"(?i)\bOLD\.rowid\b", "OLD.ctid", result)
    result = re.sub(r"(?i)\b([A-Za-z_][A-Za-z0-9_]*)\.rowid\b", r"\1.ctid", result)
    result = re.sub(r"(?i)NEW\.ctid\s*!=\s*-1", "NEW.ctid IS NOT NULL", result)
    result = re.sub(r"(?i)NEW\.ctid\s*<\s*0", "FALSE", result)
    result = re.sub(r"!=", "<>", result)
    result = re.sub(
        r"(?i)\b([A-Za-z_][A-Za-z0-9_]*\.key)\s*=\s*"
        r"([A-Za-z_][A-Za-z0-9_]*\.key)\s*\+\s*([0-9]+)\b",
        r"(\1)::BIGINT=(\2)::BIGINT+\3",
        result,
    )
    result = re.sub(
        r"(?i)\b([A-Za-z_][A-Za-z0-9_]*\.key)\s*\+\s*([0-9]+)\b",
        r"(\1)::BIGINT+\2",
        result,
    )
    result = cast_numeric_json_extracts(result)
    result = normalize_sqlite_boolean_comparisons(result)
    result = wrap_json_builders(result)
    result = re.sub(r"\s+", " ", result).strip()
    result = result.replace(
        "OPERATOR(public.!@@) '*['||chr(1)||'-'||chr(31)||']*'",
        "OPERATOR(public.!@@) ('*['||chr(1)||'-'||chr(31)||']*')",
    )
    return result


def postgres_trigger_sql(trigger: sqlite3.Row) -> list[str]:
    parsed = parse_trigger(trigger["sql"])
    trigger_name = str(parsed["name"]).strip('"')
    table_name = str(parsed["table"]).strip('"')
    timing = str(parsed["timing"]).upper()
    event = str(parsed["event"]).upper()
    body = str(parsed["body"])
    header_when = parsed["when"]
    simple_raise = raise_condition(body)
    function_name = stable_name("fn", trigger_name)
    declarations: list[str] = []
    statements: list[str] = []
    if simple_raise is not None:
        body_when, message = simple_raise
        conditions = [condition for condition in (header_when, body_when) if condition and condition != "TRUE"]
        condition = " AND ".join(f"({postgres_trigger_fragment(str(item))})" for item in conditions) or "TRUE"
        safe_message = quote_text(message)
        statements.extend(
            [
                f"  IF {condition} THEN",
                f"    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE={safe_message};",
                "  END IF;",
            ]
        )
    else:
        action_lines: list[str] = []
        for statement in split_sql_statements(body):
            ignore_or_abort = conditional_ignore_or_abort(statement)
            if ignore_or_abort is not None:
                condition, message = ignore_or_abort
                action_lines.extend(
                    [
                        f"  IF {postgres_trigger_fragment(condition)} THEN",
                        "    RETURN NULL;",
                        "  ELSE",
                        f"    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE={quote_text(message)};",
                        "  END IF;",
                    ]
                )
                continue
            change_guard = raise_condition(statement)
            if change_guard is not None:
                condition, message = change_guard
                prefix: list[str] = []
                if re.search(r"(?i)\bchanges\s*\(\s*\)", statement):
                    if not declarations:
                        declarations.append("  metahub_row_count BIGINT;")
                    condition = re.sub(r"(?i)\bchanges\s*\(\s*\)", "metahub_row_count", condition)
                    prefix.append("  GET DIAGNOSTICS metahub_row_count = ROW_COUNT;")
                action_lines.extend(
                    prefix
                    + [
                        f"  IF {postgres_trigger_fragment(condition)} THEN",
                        f"    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE={quote_text(message)};",
                        "  END IF;",
                    ]
                )
                continue
            translated = postgres_trigger_fragment(statement)
            ignored = bool(re.match(r"(?i)^INSERT\s+OR\s+IGNORE\s+INTO\b", translated))
            translated = re.sub(r"(?i)^INSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", translated)
            if ignored:
                translated += " ON CONFLICT DO NOTHING"
            action_lines.append("  " + translated + ";")
        if header_when:
            statements.append(f"  IF {postgres_trigger_fragment(str(header_when))} THEN")
            statements.extend("  " + line for line in action_lines)
            statements.append("  END IF;")
        else:
            statements.extend(action_lines)
    statements.append("  RETURN OLD;" if event == "DELETE" else "  RETURN NEW;")
    function_sql = [
        f"CREATE OR REPLACE FUNCTION {quote_identifier(function_name)}() RETURNS trigger",
        "LANGUAGE plpgsql AS $trigger$",
    ]
    if declarations:
        function_sql.append("DECLARE")
        function_sql.extend(declarations)
    function_sql.append("BEGIN")
    function_sql.extend(statements)
    function_sql.extend(["END;", "$trigger$;"])
    columns = ""
    if parsed["columns"]:
        columns = " OF " + ", ".join(
            quote_identifier(column.strip().strip('"'))
            for column in str(parsed["columns"]).split(",")
        )
    function_sql.append(
        f"CREATE TRIGGER {quote_identifier(trigger_name)} {timing} {event}{columns} "
        f"ON {quote_identifier(table_name)} FOR EACH ROW EXECUTE FUNCTION "
        f"{quote_identifier(function_name)}();"
    )
    return function_sql


arguments = parse_arguments()
temporary_source: tempfile.TemporaryDirectory[str] | None = None
if arguments.source_sqlite is None:
    temporary_source = tempfile.TemporaryDirectory(prefix="metahub-pg-baseline-")
    source_database = build_canonical_source(Path(temporary_source.name))
else:
    source_database = arguments.source_sqlite.resolve()
output_path = arguments.output.resolve()
output_path.parent.mkdir(parents=True, exist_ok=True)

connection = sqlite3.connect(source_database)
connection.row_factory = sqlite3.Row
table_names = [
    row["name"]
    for row in connection.execute(
        """
        SELECT name FROM sqlite_schema
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY rowid
        """
    )
]

manifest_sha256 = migration_manifest_sha256(connection)
execution_window = migration_execution_window(connection)
lines = [
    "-- Generated by scripts/database/generate_postgresql_baseline.py; do not edit.",
    "CREATE SCHEMA metahub_bootstrap;",
    "CREATE TABLE metahub_bootstrap.baseline_state(",
    "  singleton_id SMALLINT PRIMARY KEY CHECK(singleton_id=1),",
    "  baseline_version TEXT NOT NULL,",
    "  source_manifest_sha256 TEXT NOT NULL CHECK(length(source_manifest_sha256)=64),",
    "  generator_revision BIGINT NOT NULL CHECK(generator_revision>=1),",
    "  status TEXT NOT NULL CHECK(status IN('installing','ready')) ,",
    "  application_table_count BIGINT,",
    "  check_constraint_count BIGINT,",
    "  foreign_key_count BIGINT,",
    "  trigger_count BIGINT,",
    "  seed_row_count BIGINT,",
    "  started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),",
    "  completed_at TIMESTAMPTZ",
    ");",
    "INSERT INTO metahub_bootstrap.baseline_state(",
    "  singleton_id,baseline_version,source_manifest_sha256,generator_revision,status",
    f") VALUES(1,'{BASELINE_VERSION}','{manifest_sha256}',{GENERATOR_REVISION},'installing');",
    "BEGIN;",
    "SET LOCAL client_min_messages = warning;",
    "CREATE EXTENSION IF NOT EXISTS pgcrypto;",
    "CREATE OR REPLACE FUNCTION metahub_utc_now() RETURNS TEXT",
    "LANGUAGE sql VOLATILE AS $$",
    "  SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"');",
    "$$;",
    "CREATE OR REPLACE FUNCTION metahub_sha256(value TEXT) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE STRICT AS $$",
    "  SELECT encode(digest(value, 'sha256'), 'hex');",
    "$$;",
    "CREATE OR REPLACE FUNCTION randomblob(size BIGINT) RETURNS BYTEA",
    "LANGUAGE sql VOLATILE STRICT AS $$ SELECT gen_random_bytes(size::integer); $$;",
    "CREATE OR REPLACE FUNCTION hex(value BYTEA) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT upper(encode(value,'hex')); $$;",
    "CREATE OR REPLACE FUNCTION json_quote(value TEXT) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE AS $$ SELECT to_jsonb(value)::text; $$;",
    "CREATE OR REPLACE FUNCTION metahub_password_hash_algorithm(value TEXT) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE AS $$",
    "  SELECT CASE",
    "    WHEN length(value)<=256 AND value ~ '^\\$argon2id\\$v=19\\$m=65536,t=3,p=2\\$[A-Za-z0-9+/]{22}\\$[A-Za-z0-9+/]{43}$' THEN 'argon2id'",
    "    WHEN length(value)<=256 AND value ~ '^pbkdf2_sha256\\$200000\\$[A-Za-z0-9_-]{22}==\\$[A-Za-z0-9_-]{43}=$' THEN 'pbkdf2_sha256_legacy'",
    "    ELSE NULL END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION typeof(value ANYELEMENT) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE AS $$",
    "  SELECT CASE WHEN value IS NULL THEN 'null'",
    "    WHEN pg_typeof(value)::text IN ('smallint','integer','bigint') THEN 'integer'",
    "    WHEN pg_typeof(value)::text IN ('numeric','real','double precision') THEN 'real'",
    "    WHEN pg_typeof(value)::text = 'bytea' THEN 'blob' ELSE 'text' END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION json_valid(document TEXT) RETURNS BOOLEAN",
    "LANGUAGE plpgsql IMMUTABLE STRICT AS $$",
    "BEGIN PERFORM document::jsonb; RETURN TRUE;",
    "EXCEPTION WHEN others THEN RETURN FALSE; END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION metahub_json_value(document TEXT, path TEXT) RETURNS JSONB",
    "LANGUAGE plpgsql IMMUTABLE STRICT AS $$",
    "DECLARE normalized TEXT; result JSONB; BEGIN",
    "  normalized := regexp_replace(path, '^\\$\\.?', '');",
    "  normalized := regexp_replace(normalized, '\\[([0-9]+)\\]', '.\\1', 'g');",
    "  normalized := trim(both '.' from normalized);",
    "  IF normalized = '' THEN result := document::jsonb;",
    "  ELSE result := document::jsonb #> string_to_array(normalized, '.'); END IF;",
    "  RETURN result; EXCEPTION WHEN others THEN RETURN NULL; END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION json_extract(document TEXT, path TEXT) RETURNS TEXT",
    "LANGUAGE plpgsql IMMUTABLE STRICT AS $$",
    "DECLARE",
    "  normalized TEXT;",
    "  value JSONB;",
    "BEGIN",
    "  normalized := regexp_replace(path, '^\\$\\.?', '');",
    "  normalized := regexp_replace(normalized, '\\[([0-9]+)\\]', '.\\1', 'g');",
    "  normalized := trim(both '.' from normalized);",
    "  IF normalized = '' THEN",
    "    value := document::jsonb;",
    "  ELSE",
    "    value := document::jsonb #> string_to_array(normalized, '.');",
    "  END IF;",
    "  IF value IS NULL OR value = 'null'::jsonb THEN RETURN NULL; END IF;",
    "  IF jsonb_typeof(value) = 'string' THEN RETURN value #>> '{}'; END IF;",
    "  IF jsonb_typeof(value) = 'boolean' THEN",
    "    RETURN CASE WHEN value = 'true'::jsonb THEN '1' ELSE '0' END;",
    "  END IF;",
    "  RETURN value::text;",
    "EXCEPTION WHEN others THEN",
    "  RETURN NULL;",
    "END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION json_type(document TEXT, path TEXT DEFAULT '$') RETURNS TEXT",
    "LANGUAGE plpgsql IMMUTABLE STRICT AS $$",
    "DECLARE value JSONB; kind TEXT; BEGIN",
    "  value := metahub_json_value(document, path);",
    "  IF value IS NULL THEN RETURN NULL; END IF;",
    "  kind := jsonb_typeof(value);",
    "  IF kind = 'string' THEN RETURN 'text'; END IF;",
    "  IF kind = 'number' THEN",
    "    RETURN CASE WHEN value::text ~ '^-?[0-9]+$' THEN 'integer' ELSE 'real' END;",
    "  END IF;",
    "  IF kind = 'boolean' THEN RETURN CASE WHEN value = 'true'::jsonb THEN 'true' ELSE 'false' END; END IF;",
    "  RETURN kind; EXCEPTION WHEN others THEN RETURN NULL; END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION json_array_length(document TEXT, path TEXT DEFAULT '$') RETURNS BIGINT",
    "LANGUAGE plpgsql IMMUTABLE STRICT AS $$",
    "DECLARE value JSONB; BEGIN value := metahub_json_value(document, path);",
    "  IF jsonb_typeof(value) <> 'array' THEN RETURN NULL; END IF;",
    "  RETURN jsonb_array_length(value); EXCEPTION WHEN others THEN RETURN NULL; END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION metahub_json_scalar(value JSONB) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE AS $$",
    "  SELECT CASE WHEN value IS NULL OR value='null'::jsonb THEN NULL",
    "    WHEN jsonb_typeof(value)='string' THEN value #>> '{}'",
    "    WHEN jsonb_typeof(value)='boolean' THEN CASE WHEN value='true'::jsonb THEN '1' ELSE '0' END",
    "    ELSE value::text END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION metahub_json_type(value JSONB) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE AS $$",
    "  SELECT CASE jsonb_typeof(value) WHEN 'string' THEN 'text' WHEN 'number' THEN",
    "    CASE WHEN value::text ~ '^-?[0-9]+$' THEN 'integer' ELSE 'real' END",
    "    WHEN 'boolean' THEN CASE WHEN value='true'::jsonb THEN 'true' ELSE 'false' END",
    "    ELSE jsonb_typeof(value) END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION metahub_json_atom(value JSONB) RETURNS NUMERIC",
    "LANGUAGE sql IMMUTABLE AS $$",
    "  SELECT CASE WHEN jsonb_typeof(value)='number' THEN value::text::numeric",
    "    WHEN value='true'::jsonb THEN 1 WHEN value='false'::jsonb THEN 0 ELSE NULL END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION json_each(document TEXT)",
    "RETURNS TABLE(key TEXT,value TEXT,type TEXT,atom NUMERIC) LANGUAGE sql IMMUTABLE STRICT AS $$",
    "  WITH source AS (SELECT document::jsonb AS value)",
    "  SELECT item.key,metahub_json_scalar(item.value),metahub_json_type(item.value),metahub_json_atom(item.value)",
    "  FROM source CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(source.value)='object' THEN source.value ELSE '{}'::jsonb END) item",
    "  UNION ALL",
    "  SELECT (item.ordinality-1)::text,metahub_json_scalar(item.value),metahub_json_type(item.value),metahub_json_atom(item.value)",
    "  FROM source CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(source.value)='array' THEN source.value ELSE '[]'::jsonb END) WITH ORDINALITY item(value,ordinality);",
    "$$;",
    "CREATE OR REPLACE FUNCTION json_each(document TEXT,path TEXT)",
    "RETURNS TABLE(key TEXT,value TEXT,type TEXT,atom NUMERIC) LANGUAGE sql IMMUTABLE STRICT AS $$",
    "  SELECT item.key,item.value,item.type,item.atom",
    "  FROM json_each(COALESCE(metahub_json_value(document,path),'null'::jsonb)::text) item;",
    "$$;",
    "CREATE OR REPLACE FUNCTION json_tree(document TEXT)",
    "RETURNS TABLE(key TEXT,value TEXT,type TEXT,atom NUMERIC) LANGUAGE sql IMMUTABLE STRICT AS $$",
    "  WITH RECURSIVE walk(key,node) AS (",
    "    SELECT NULL::text,document::jsonb",
    "    UNION ALL",
    "    SELECT child.key,child.node FROM walk CROSS JOIN LATERAL (",
    "      SELECT item.key,item.value AS node FROM jsonb_each(CASE WHEN jsonb_typeof(walk.node)='object' THEN walk.node ELSE '{}'::jsonb END) item",
    "      UNION ALL",
    "      SELECT (item.ordinality-1)::text,item.value FROM jsonb_array_elements(CASE WHEN jsonb_typeof(walk.node)='array' THEN walk.node ELSE '[]'::jsonb END) WITH ORDINALITY item(value,ordinality)",
    "    ) child)",
    "  SELECT walk.key,metahub_json_scalar(walk.node),metahub_json_type(walk.node),metahub_json_atom(walk.node) FROM walk;",
    "$$;",
    "CREATE OR REPLACE FUNCTION metahub_json_group_array_sfunc(state JSONB,value TEXT) RETURNS JSONB",
    "LANGUAGE sql IMMUTABLE AS $$ SELECT COALESCE(state,'[]'::jsonb)||jsonb_build_array(value); $$;",
    "CREATE OR REPLACE FUNCTION metahub_jsonb_text(value JSONB) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT value::text; $$;",
    "CREATE AGGREGATE json_group_array(TEXT) (",
    "  SFUNC=metahub_json_group_array_sfunc,STYPE=JSONB,INITCOND='[]',FINALFUNC=metahub_jsonb_text",
    ");",
    "CREATE OR REPLACE FUNCTION metahub_sum_boolean_sfunc(state BIGINT,value BOOLEAN) RETURNS BIGINT",
    "LANGUAGE sql IMMUTABLE AS $$ SELECT CASE WHEN value IS NULL THEN state WHEN state IS NULL THEN value::integer::bigint ELSE state+value::integer END; $$;",
    "CREATE AGGREGATE sum(BOOLEAN) (SFUNC=metahub_sum_boolean_sfunc,STYPE=BIGINT);",
    "CREATE OR REPLACE FUNCTION metahub_boolean_equal_integer(left_value BOOLEAN,right_value INTEGER) RETURNS BOOLEAN",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT left_value=(right_value<>0); $$;",
    "CREATE OPERATOR public.= (LEFTARG=BOOLEAN,RIGHTARG=INTEGER,FUNCTION=metahub_boolean_equal_integer);",
    "CREATE OR REPLACE FUNCTION json_remove(document TEXT, VARIADIC paths TEXT[]) RETURNS TEXT",
    "LANGUAGE plpgsql IMMUTABLE STRICT AS $$",
    "DECLARE result JSONB := document::jsonb; path TEXT; normalized TEXT; BEGIN",
    "  FOREACH path IN ARRAY paths LOOP",
    "    normalized := regexp_replace(path, '^\\$\\.?', '');",
    "    normalized := regexp_replace(normalized, '\\[([0-9]+)\\]', '.\\1', 'g');",
    "    result := result #- string_to_array(trim(both '.' from normalized), '.');",
    "  END LOOP; RETURN result::text; EXCEPTION WHEN others THEN RETURN NULL; END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION metahub_json(document TEXT) RETURNS TEXT",
    "LANGUAGE plpgsql IMMUTABLE STRICT AS $$",
    "DECLARE result TEXT; BEGIN",
    "  PERFORM document::json;",
    "  -- Validate with the native parser, then strip only whitespace outside strings.",
    "  -- jsonb serialization changes key order, duplicate keys, escapes and numeric spelling.",
    "  SELECT string_agg(token[1],'' ORDER BY ordinal) INTO result",
    r'''  FROM regexp_matches(document,$tokens$"(?:[^"\\]|\\.)*"|[^ \t\r\n"]+$tokens$,'g')''',
    "    WITH ORDINALITY AS tokens(token,ordinal);",
    "  RETURN result; EXCEPTION WHEN others THEN RETURN NULL; END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION metahub_glob(value TEXT, pattern TEXT) RETURNS BOOLEAN",
    "LANGUAGE plpgsql IMMUTABLE STRICT AS $$",
    "DECLARE regex TEXT := '^'; index BIGINT := 1; character TEXT; BEGIN",
    "  WHILE index <= length(pattern) LOOP character := substr(pattern,index,1);",
    "    IF character='*' THEN regex := regex||'.*';",
    "    ELSIF character='?' THEN regex := regex||'.';",
    "    ELSIF substr(pattern,index,11)='[[:space:]]' THEN",
    "      regex := regex||'[[:space:]]'; index := index+10;",
    "    ELSIF character='[' AND strpos(substr(pattern,index),']')=0 THEN regex := regex||'\\[';",
    "    ELSIF character='[' THEN",
    "      regex := regex||substr(pattern,index,strpos(substr(pattern,index),']'));",
    "      index := index+strpos(substr(pattern,index),']')-1;",
    "    ELSIF character IN ('.','+','(',')','{','}','|','^','$','\\') THEN regex := regex||'\\'||character;",
    "    ELSE regex := regex||character; END IF; index := index+1;",
    "  END LOOP; RETURN value ~ (regex||'$'); END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION glob(pattern TEXT, value TEXT) RETURNS BOOLEAN",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT metahub_glob(value, pattern); $$;",
    "CREATE OPERATOR public.@@ (LEFTARG=TEXT, RIGHTARG=TEXT, FUNCTION=metahub_glob);",
    "CREATE OR REPLACE FUNCTION metahub_not_glob(value TEXT, pattern TEXT) RETURNS BOOLEAN",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT NOT metahub_glob(value,pattern); $$;",
    "CREATE OPERATOR public.!@@ (LEFTARG=TEXT, RIGHTARG=TEXT, FUNCTION=metahub_not_glob);",
    "CREATE OR REPLACE FUNCTION metahub_timestamp(value TEXT, modifiers TEXT[]) RETURNS TIMESTAMPTZ",
    "LANGUAGE plpgsql STABLE STRICT AS $$",
    "DECLARE result TIMESTAMPTZ; modifier TEXT; amount DOUBLE PRECISION; unit TEXT; BEGIN",
    "  IF lower(value)='now' THEN result := clock_timestamp(); ELSE result := value::timestamptz; END IF;",
    "  FOREACH modifier IN ARRAY modifiers LOOP",
    "    IF modifier='+0 seconds' THEN CONTINUE; END IF;",
    "    amount := substring(modifier FROM '^([+-]?[0-9]+(?:\\.[0-9]+)?)')::double precision;",
    "    unit := lower(substring(modifier FROM '(seconds?|minutes?|hours?|days?)$'));",
    "    IF unit LIKE 'second%' THEN result := result + amount*interval '1 second';",
    "    ELSIF unit LIKE 'minute%' THEN result := result + amount*interval '1 minute';",
    "    ELSIF unit LIKE 'hour%' THEN result := result + amount*interval '1 hour';",
    "    ELSIF unit LIKE 'day%' THEN result := result + amount*interval '1 day';",
    "    ELSE RETURN NULL; END IF;",
    "  END LOOP; RETURN result; EXCEPTION WHEN others THEN RETURN NULL; END;",
    "$$;",
    "CREATE OR REPLACE FUNCTION unixepoch(value TEXT, VARIADIC modifiers TEXT[]) RETURNS BIGINT",
    "LANGUAGE sql STABLE STRICT AS $$ SELECT extract(epoch FROM metahub_timestamp(value,modifiers))::bigint; $$;",
    "CREATE OR REPLACE FUNCTION unixepoch(value TEXT) RETURNS BIGINT",
    "LANGUAGE sql STABLE STRICT AS $$ SELECT extract(epoch FROM metahub_timestamp(value,ARRAY[]::TEXT[]))::bigint; $$;",
    "CREATE OR REPLACE FUNCTION julianday(value TEXT, VARIADIC modifiers TEXT[]) RETURNS DOUBLE PRECISION",
    "LANGUAGE sql STABLE STRICT AS $$ SELECT extract(epoch FROM metahub_timestamp(value,modifiers))/86400.0+2440587.5; $$;",
    "CREATE OR REPLACE FUNCTION julianday(value TEXT) RETURNS DOUBLE PRECISION",
    "LANGUAGE sql STABLE STRICT AS $$ SELECT extract(epoch FROM metahub_timestamp(value,ARRAY[]::TEXT[]))/86400.0+2440587.5; $$;",
    "CREATE OR REPLACE FUNCTION strftime(format TEXT, value TEXT, VARIADIC modifiers TEXT[]) RETURNS TEXT",
    "LANGUAGE plpgsql STABLE STRICT AS $$ DECLARE stamp TIMESTAMPTZ; BEGIN",
    "  stamp := metahub_timestamp(value,modifiers); IF stamp IS NULL THEN RETURN NULL; END IF;",
    "  IF format='%s' THEN RETURN extract(epoch FROM stamp)::bigint::text; END IF;",
    "  IF format='%S' THEN RETURN to_char(stamp AT TIME ZONE 'UTC','SS'); END IF;",
    "  IF format='%Y-%m-%dT%H:%M:' THEN RETURN to_char(stamp AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:'); END IF;",
    "  IF format='%Y-%m-%dT%H:%M:%fZ' THEN RETURN to_char(stamp AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"'); END IF;",
    "  IF format='%Y-%m-%dT%H:%M:%SZ' THEN RETURN to_char(stamp AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'); END IF;",
    "  RETURN NULL; END; $$;",
    "CREATE OR REPLACE FUNCTION strftime(format TEXT, value TEXT) RETURNS TEXT",
    "LANGUAGE sql STABLE STRICT AS $$ SELECT strftime(format,value,VARIADIC ARRAY[]::TEXT[]); $$;",
    "CREATE OR REPLACE FUNCTION date(value TEXT, VARIADIC modifiers TEXT[]) RETURNS TEXT",
    "LANGUAGE sql STABLE STRICT AS $$ SELECT to_char(metahub_timestamp(value,modifiers) AT TIME ZONE 'UTC','YYYY-MM-DD'); $$;",
    "CREATE OR REPLACE FUNCTION date(value TEXT) RETURNS TEXT",
    "LANGUAGE sql STABLE STRICT AS $$ SELECT to_char(metahub_timestamp(value,ARRAY[]::TEXT[]) AT TIME ZONE 'UTC','YYYY-MM-DD'); $$;",
    "CREATE OR REPLACE FUNCTION datetime(value TEXT, VARIADIC modifiers TEXT[]) RETURNS TEXT",
    "LANGUAGE sql STABLE STRICT AS $$ SELECT to_char(metahub_timestamp(value,modifiers) AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS'); $$;",
    "CREATE OR REPLACE FUNCTION datetime(value TEXT) RETURNS TEXT",
    "LANGUAGE sql STABLE STRICT AS $$ SELECT to_char(metahub_timestamp(value,ARRAY[]::TEXT[]) AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS'); $$;",
    "CREATE OR REPLACE FUNCTION instr(value TEXT, needle TEXT) RETURNS BIGINT",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT strpos(value,needle)::bigint; $$;",
    "CREATE OR REPLACE FUNCTION substr(value TEXT, start_at BIGINT) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT pg_catalog.substr(value,start_at::integer); $$;",
    "CREATE OR REPLACE FUNCTION substr(value TEXT, start_at BIGINT, count BIGINT) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT pg_catalog.substr(value,start_at::integer,count::integer); $$;",
    "CREATE OR REPLACE FUNCTION chr(value BIGINT) RETURNS TEXT LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT pg_catalog.chr(value::integer); $$;",
    "CREATE OR REPLACE FUNCTION printf(format TEXT, value BIGINT) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT CASE format WHEN '%02d' THEN lpad(value::text,2,'0') ELSE value::text END; $$;",
    "CREATE OR REPLACE FUNCTION printf(format TEXT, value INTEGER) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT printf(format,value::bigint); $$;",
    "CREATE OR REPLACE FUNCTION printf(format TEXT, value NUMERIC) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT CASE format WHEN '%.6f' THEN to_char(value,'FM999999999999999999999999990.000000') ELSE value::text END; $$;",
    "CREATE OR REPLACE FUNCTION printf(format TEXT, first BIGINT, second BIGINT) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT CASE format WHEN '%d.%06d' THEN first::text||'.'||lpad(second::text,6,'0') ELSE first::text||second::text END; $$;",
    "CREATE OR REPLACE FUNCTION printf(format TEXT, first NUMERIC, second NUMERIC) RETURNS TEXT",
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT printf(format,first::bigint,second::bigint); $$;",
]

columns_by_table: dict[str, list[sqlite3.Row]] = {}
for table_name in table_names:
    escaped = table_name.replace("'", "''")
    columns = connection.execute(f"PRAGMA table_xinfo('{escaped}')").fetchall()
    columns_by_table[table_name] = columns
    definitions: list[str] = []
    primary_key = sorted(
        ((int(column["pk"]), column["name"]) for column in columns if column["pk"]),
        key=lambda item: item[0],
    )
    for column in columns:
        target_type = postgres_type(str(column["type"] or ""))
        column_name = str(column["name"])
        if target_type == "BIGINT":
            if column_name not in NON_NUMERIC_COLUMN_NAMES:
                NUMERIC_COLUMN_NAMES.add(column_name)
        else:
            NON_NUMERIC_COLUMN_NAMES.add(column_name)
            NUMERIC_COLUMN_NAMES.discard(column_name)
        definition = f"  {quote_identifier(column['name'])} {target_type}"
        if int(column["notnull"]):
            definition += " NOT NULL"
        default = postgres_default(column["dflt_value"], target_type)
        if default is not None:
            definition += f" DEFAULT {default}"
        definitions.append(definition)
    if primary_key:
        columns_sql = ", ".join(quote_identifier(item[1]) for item in primary_key)
        definitions.append(f"  PRIMARY KEY ({columns_sql})")
    lines.append(f"CREATE TABLE {quote_identifier(table_name)} (\n")
    lines.append(",\n".join(definitions))
    lines.append("\n);")

lines.append("-- SQLite CHECK constraints translated to PostgreSQL-native expressions.")
check_constraints: list[tuple[str, str]] = []
for table_name in table_names:
    source = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    for index, check in enumerate(balanced_checks(source["sql"]), start=1):
        constraint_name = stable_name("ck", table_name, str(index))
        check_constraints.append((table_name, constraint_name))
        lines.append(
            f"ALTER TABLE {quote_identifier(table_name)} ADD CONSTRAINT "
            f"{quote_identifier(constraint_name)} CHECK ({postgres_expression(check)}) NOT VALID;"
        )

lines.append("-- Seed and migration facts from the reviewed SQLite canonical head.")
seed_row_count = 0
for table_name in table_names:
    columns = [column["name"] for column in columns_by_table[table_name] if not column["hidden"]]
    if not columns:
        continue
    selected = ", ".join(quote_identifier(column) for column in columns)
    rows = connection.execute(
        f"SELECT {selected} FROM {quote_identifier(table_name)}"
    ).fetchall()
    if not rows:
        continue
    seed_row_count += len(rows)
    lines.append(f"INSERT INTO {quote_identifier(table_name)} ({selected}) VALUES")
    tuples = [
        "("
        + ", ".join(
            literal(
                canonical_seed_value(
                    table_name,
                    str(columns[index]),
                    row[index],
                    execution_window,
                )
            )
            for index in range(len(columns))
        )
        + ")"
        for row in rows
    ]
    lines.append(",\n".join(tuples) + ";")

lines.append("-- Canonical unique and explicit indexes.")
created_indexes: set[tuple[str, tuple[str, ...], bool]] = set()
for table_name in table_names:
    escaped = table_name.replace("'", "''")
    for index in connection.execute(f"PRAGMA index_list('{escaped}')").fetchall():
        index_name = index["name"]
        if index_name.startswith("sqlite_autoindex_"):
            if not int(index["unique"]):
                continue
            columns = connection.execute(
                f"PRAGMA index_info('{index_name.replace("'", "''")}')"
            ).fetchall()
            column_names = tuple(row["name"] for row in columns if row["name"] is not None)
            if not column_names:
                continue
            key = (table_name, column_names, True)
            if key in created_indexes:
                continue
            created_indexes.add(key)
            generated_name = stable_name("uq", table_name, *column_names)
            joined = ", ".join(quote_identifier(column) for column in column_names)
            lines.append(
                f"CREATE UNIQUE INDEX {quote_identifier(generated_name)} "
                f"ON {quote_identifier(table_name)} ({joined});"
            )
            continue
        source = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        if source is None or source["sql"] is None:
            continue
        sql = source["sql"].strip()
        sql = re.sub(r"(?i)\s+COLLATE\s+NOCASE", "", sql)
        sql = re.sub(
            r"(?is)(,\s*)(CASE\b.*?\bEND)(\s*,)",
            lambda match: match.group(1) + "(" + match.group(2) + ")" + match.group(3),
            sql,
        )
        lines.append(sql + ";")

lines.append("-- Composite ownership and referential constraints.")
foreign_key_count = 0
for table_name in table_names:
    escaped = table_name.replace("'", "''")
    foreign_keys = connection.execute(f"PRAGMA foreign_key_list('{escaped}')").fetchall()
    grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for foreign_key in foreign_keys:
        grouped[int(foreign_key["id"])].append(foreign_key)
    for foreign_key_id, items in sorted(grouped.items()):
        foreign_key_count += 1
        items.sort(key=lambda item: int(item["seq"]))
        target = items[0]["table"]
        source_columns = ", ".join(quote_identifier(item["from"]) for item in items)
        target_columns = ", ".join(quote_identifier(item["to"]) for item in items)
        constraint_name = stable_name("fk", table_name, str(foreign_key_id), target)
        clause = (
            f"ALTER TABLE {quote_identifier(table_name)} ADD CONSTRAINT "
            f"{quote_identifier(constraint_name)} FOREIGN KEY ({source_columns}) "
            f"REFERENCES {quote_identifier(target)} ({target_columns})"
        )
        on_update = str(items[0]["on_update"])
        on_delete = str(items[0]["on_delete"])
        if on_update != "NO ACTION":
            clause += f" ON UPDATE {on_update}"
        if on_delete != "NO ACTION":
            clause += f" ON DELETE {on_delete}"
        lines.append(clause + ";")

lines.extend(
    [
        "COMMIT;",
        "BEGIN;",
        "SET LOCAL client_min_messages = warning;",
        "-- SQLite row guards and side effects translated to native trigger functions.",
    ]
)
trigger_rows = connection.execute(
    "SELECT name,tbl_name,sql FROM sqlite_schema "
    "WHERE type='trigger' AND sql IS NOT NULL ORDER BY rowid"
).fetchall()
for trigger_index, trigger in enumerate(trigger_rows, start=1):
    lines.extend(postgres_trigger_sql(trigger))
    if trigger_index % 250 == 0:
        lines.extend(["COMMIT;", "BEGIN;", "SET LOCAL client_min_messages = warning;"])

lines.extend(
    [
        "COMMIT;",
        "BEGIN;",
        "SET LOCAL client_min_messages = warning;",
        "-- Prove every translated CHECK against the canonical baseline rows.",
    ]
)
for check_index, (table_name, constraint_name) in enumerate(check_constraints, start=1):
    lines.append(
        f"ALTER TABLE {quote_identifier(table_name)} VALIDATE CONSTRAINT "
        f"{quote_identifier(constraint_name)};"
    )
    if check_index % 500 == 0:
        lines.extend(["COMMIT;", "BEGIN;", "SET LOCAL client_min_messages = warning;"])

lines.extend(
    [
        "DO $baseline$",
        "DECLARE",
        "  actual_count BIGINT;",
        "  exact_seed_rows BIGINT := 0;",
        "  table_row RECORD;",
        "BEGIN",
        "  SELECT count(*) INTO actual_count FROM pg_tables WHERE schemaname='public';",
        f"  IF actual_count!={len(table_names)} THEN RAISE EXCEPTION 'baseline_table_count_invalid'; END IF;",
        "  SELECT count(*) INTO actual_count FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' AND c.contype='c' AND c.convalidated;",
        f"  IF actual_count!={len(check_constraints)} THEN RAISE EXCEPTION 'baseline_check_count_invalid'; END IF;",
        "  SELECT count(*) INTO actual_count FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' AND c.contype='f' AND c.convalidated;",
        f"  IF actual_count!={foreign_key_count} THEN RAISE EXCEPTION 'baseline_foreign_key_count_invalid'; END IF;",
        "  SELECT count(*) INTO actual_count FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal AND t.tgenabled='O';",
        f"  IF actual_count!={len(trigger_rows)} THEN RAISE EXCEPTION 'baseline_trigger_count_invalid'; END IF;",
        "  SELECT count(*) INTO actual_count FROM schema_migrations WHERE status='applied';",
        f"  IF actual_count!=139 OR (SELECT max(version) FROM schema_migrations)!='{BASELINE_VERSION}' THEN RAISE EXCEPTION 'baseline_migration_head_invalid'; END IF;",
        "  FOR table_row IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP",
        "    EXECUTE format('SELECT count(*) FROM %I.%I','public',table_row.tablename) INTO actual_count;",
        "    exact_seed_rows := exact_seed_rows + actual_count;",
        "  END LOOP;",
        f"  IF exact_seed_rows!={seed_row_count} THEN RAISE EXCEPTION 'baseline_seed_count_invalid'; END IF;",
        "  UPDATE metahub_bootstrap.baseline_state SET",
        "    status='ready',",
        f"    application_table_count={len(table_names)},",
        f"    check_constraint_count={len(check_constraints)},",
        f"    foreign_key_count={foreign_key_count},",
        f"    trigger_count={len(trigger_rows)},",
        f"    seed_row_count={seed_row_count},",
        "    completed_at=clock_timestamp()",
        "  WHERE singleton_id=1 AND status='installing';",
        "  IF NOT FOUND THEN RAISE EXCEPTION 'baseline_state_transition_invalid'; END IF;",
        "END;",
        "$baseline$;",
        "COMMIT;",
        "ANALYZE;",
    ]
)
output_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
connection.close()
if temporary_source is not None:
    temporary_source.cleanup()
print(
    f"generated={output_path} bytes={output_path.stat().st_size} "
    f"tables={len(table_names)} checks={len(check_constraints)} "
    f"foreign_keys={foreign_key_count} triggers={len(trigger_rows)} seeds={seed_row_count}"
)
