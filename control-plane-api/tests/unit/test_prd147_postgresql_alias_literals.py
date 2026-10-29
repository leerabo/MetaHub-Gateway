"""Run generator definitions without executing its baseline-writing entry point."""

import ast
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def generator():
    path = Path(__file__).resolve().parents[3] / "scripts/database/generate_postgresql_baseline.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definitions = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "arguments" for target in node.targets
        ):
            break
        definitions.append(node)
    namespace = {"__file__": str(path), "__name__": "postgres_generator_definitions"}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("table,alias", [
    ("users", "user"), ("session_risk_freezes", "freeze"), ("access_token_grants", "grant"),
])
def test_alias_references_change_without_rewriting_permissions_events_or_text(
    generator, table, alias,
):
    sql = (
        f"SELECT {alias}.account_id FROM {table} {alias} "
        f"WHERE permission.code='{alias}.manage' AND event.type='{alias}.invited' "
        f"AND note='FROM {table} {alias} {alias}.account_id'"
    )
    result = generator["postgres_trigger_fragment"](sql)
    assert f"SELECT {alias}_row.account_id FROM {table} {alias}_row " in result
    assert f"permission.code='{alias}.manage'" in result
    assert f"event.type='{alias}.invited'" in result
    assert f"note='FROM {table} {alias} {alias}.account_id'" in result


@pytest.mark.parametrize("literal", [
    "'quoted ''user.manage'''",
    "'{\"permission\":\"user.manage\"}'",
    "'\u4e2d\u6587user.invited'",
    "'user.manage -- still a string'",
])
def test_quoted_literal_bytes_survive_alias_conversion(generator, literal):
    result = generator["postgres_trigger_fragment"](
        f"SELECT {literal}, user.account_id FROM users user",
    )
    assert result == f"SELECT {literal}, user_row.account_id FROM users user_row"


def test_quoted_identifiers_and_explicit_alias_keyword(generator):
    result = generator["postgres_trigger_fragment"](
        'SELECT "user.id", user.account_id FROM users AS user',
    )
    assert result == 'SELECT "user.id", user_row.account_id FROM users AS user_row'
