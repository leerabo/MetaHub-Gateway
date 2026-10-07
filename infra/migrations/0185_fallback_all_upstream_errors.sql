-- metahub: fallback-all-upstream-errors-v1
-- An enabled fallback policy must continue through every normalized upstream
-- provider failure. Local request construction failures remain terminal.

BEGIN IMMEDIATE;

UPDATE fallback_policies
SET condition_expr = json_insert(condition_expr, '$[#]', 'upstream_any_error'),
    version = version + 1,
    updated_at = datetime('now')
WHERE status='enabled'
  AND json_valid(condition_expr)
  AND json_type(condition_expr)='array'
  AND NOT EXISTS (
    SELECT 1
    FROM json_each(fallback_policies.condition_expr)
    WHERE json_each.value='upstream_any_error'
  );

COMMIT;
