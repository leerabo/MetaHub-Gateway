"""Prepare a guarded forward repair from an isolated, migrated PG inventory.

This writes a reviewable SQL artifact only. It never connects to a database.
The historical baseline and later retirement guards remain unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

COMPARISON = re.compile(
    r"NEW\.ctid\s*(<>|=|IS DISTINCT FROM|IS NOT DISTINCT FROM)\s*OLD\.ctid"
)


def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-output", type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    if inventory["status"] != "inventory_only":
        raise ValueError("Expected an isolated current-schema inventory")
    changes = []
    seen = set()
    for guard in inventory["guards"]:
        definition = guard["definition"]
        found = list(COMPARISON.finditer(definition))
        if not found:
            continue
        expected_count = 2 if guard["function_name"] == (
            "fn_trg_provider_error_map_events_state_update_prd056"
        ) else 1
        if len(found) != expected_count or guard["tgtype"] != 19:
            raise ValueError("Unexpected identity guard shape/event")
        name = guard["function_name"]
        if name in seen:
            raise ValueError("Shared function requires individual review")
        seen.add(name)
        keys = guard["primary_key"]
        if not keys or any(not re.fullmatch(r"[a-z_][a-z_0-9]*", key) for key in keys):
            raise ValueError("A logical primary key is required")
        operator = found[0][1]
        if any(match[0] != found[0][0] for match in found):
            raise ValueError("Mixed identity comparisons require individual review")
        # All keys must already have the same immutable-identity predicate.
        # PKs are NOT NULL; <> and IS DISTINCT FROM are equivalent for them.
        equivalent = "(?:<>|IS DISTINCT FROM)" if operator in ("<>", "IS DISTINCT FROM") else (
            "(?:=|IS NOT DISTINCT FROM)"
        )
        for key in keys:
            if len(re.findall(r"NEW\." + re.escape(key) + r"\s*" + equivalent
                              + r"\s*OLD\." + re.escape(key), definition)) < expected_count:
                raise ValueError(f"Logical identity predicate needs review: {name}")
        new_expression = (
            "ROW(" + ",".join("NEW." + key for key in keys) + ") " + operator
            + " ROW(" + ",".join("OLD." + key for key in keys) + ")"
        )
        body_parts = definition.split("$function$")
        if len(body_parts) != 3 or definition.count("NEW.ctid") != expected_count:
            raise ValueError("Function body requires individual review")
        changes.append({
            "function": name, "table": guard["table_name"], "primary_key": keys,
            "source_md5": hashlib.md5(body_parts[1].encode()).hexdigest(),
            "old_expression": found[0][0], "new_expression": new_expression,
            "occurrences": expected_count,
        })
    if len(changes) != 103:
        raise ValueError("Inventory changed; review it before preparing the repair")
    values = []
    for change in changes:
        fields = [quote(change[key]) for key in
                  ("function", "table", "source_md5", "old_expression", "new_expression")]
        fields.append("ARRAY[" + ",".join(map(quote, change["primary_key"])) + "]::text[]")
        fields.append(str(change["occurrences"]))
        values.append("    (" + ", ".join(fields) + ")")
    header = """-- Repair only the 103 reviewed, still-attached BEFORE UPDATE guards.
-- SQLite rowid is stable; PostgreSQL ctid changes on every physical UPDATE
-- and NEW.ctid is unassigned in BEFORE triggers. Compare existing logical
-- primary keys instead. Their immutable predicates already exist in each
-- function; every lease/state/actor/audit/content check remains byte-identical.
-- No rows, constraints, retired guards, or historical migrations are changed.
-- Refuse an unexpected body, binding or primary key rather than overwrite it.
DO $repair$
DECLARE
  patch record;
  function_oid oid;
  original_body text;
  original_definition text;
  actual_keys text[];
  bindings integer;
BEGIN
  FOR patch IN SELECT * FROM (VALUES
"""
    footer = """
  ) AS reviewed(function_name,table_name,source_md5,old_expression,
                new_expression,key_columns,occurrences)
  LOOP
    function_oid := to_regprocedure(format('public.%I()',patch.function_name));
    IF function_oid IS NULL THEN
      RAISE EXCEPTION 'logical_identity_repair_function_missing';
    END IF;
    SELECT prosrc,pg_get_functiondef(oid) INTO original_body,original_definition
      FROM pg_proc WHERE oid=function_oid;
    IF md5(original_body)<>patch.source_md5 OR
       (length(original_body)-length(replace(original_body,patch.old_expression,'')))
         <>length(patch.old_expression)*patch.occurrences THEN
      RAISE EXCEPTION 'logical_identity_repair_source_mismatch';
    END IF;
    SELECT count(*) INTO bindings FROM pg_trigger t
      JOIN pg_class c ON c.oid=t.tgrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE t.tgfoid=function_oid AND NOT t.tgisinternal AND t.tgtype=19
        AND n.nspname='public' AND c.relname=patch.table_name;
    IF bindings<>1 THEN
      RAISE EXCEPTION 'logical_identity_repair_binding_mismatch';
    END IF;
    SELECT ARRAY(SELECT a.attname::text FROM pg_index i
      JOIN unnest(i.indkey) WITH ORDINALITY k(attnum,n) ON true
      JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
      WHERE i.indrelid=to_regclass(format('public.%I',patch.table_name))
        AND i.indisprimary ORDER BY k.n) INTO actual_keys;
    IF actual_keys IS DISTINCT FROM patch.key_columns THEN
      RAISE EXCEPTION 'logical_identity_repair_key_mismatch';
    END IF;
    EXECUTE replace(original_definition,patch.old_expression,patch.new_expression);
  END LOOP;
END;
$repair$;
"""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(header + ",\n".join(values) + footer, encoding="utf-8", newline="\n")
    args.review_output.parent.mkdir(parents=True, exist_ok=True)
    args.review_output.write_text(
        json.dumps({"status": "prepared_not_applied", "changes": changes}, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"status": "prepared_not_applied", "functions": len(changes),
                      "tables": len({change["table"] for change in changes})}))


if __name__ == "__main__":
    main()
