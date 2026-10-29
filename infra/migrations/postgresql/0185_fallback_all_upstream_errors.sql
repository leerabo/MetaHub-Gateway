-- metahub: fallback-all-upstream-errors-v1
-- Keep the PostgreSQL schema's JSON contract as text, matching SQLite.

UPDATE fallback_policies
SET condition_expr = (
      SELECT jsonb_agg(item.value ORDER BY item.ordinality)::text
      FROM jsonb_array_elements(
        (fallback_policies.condition_expr::jsonb) || '["upstream_any_error"]'::jsonb
      ) WITH ORDINALITY AS item(value, ordinality)
    ),
    version = version + 1,
    updated_at = to_char(current_timestamp AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
WHERE status='enabled'
  AND jsonb_typeof(condition_expr::jsonb)='array'
  AND NOT EXISTS (
    SELECT 1
    FROM jsonb_array_elements(condition_expr::jsonb) AS item(value)
    WHERE item.value #>> '{}' = 'upstream_any_error'
  );
