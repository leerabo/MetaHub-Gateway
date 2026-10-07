-- metahub:expected-version-rollback-v1

BEGIN IMMEDIATE;

UPDATE fallback_policies
SET condition_expr = (
      SELECT json_group_array(value)
      FROM json_each(fallback_policies.condition_expr)
      WHERE value <> 'upstream_any_error'
    ),
    version = CASE WHEN version > 1 THEN version - 1 ELSE 1 END,
    updated_at = datetime('now')
WHERE status='enabled'
  AND json_valid(condition_expr)
  AND EXISTS (
    SELECT 1
    FROM json_each(fallback_policies.condition_expr)
    WHERE json_each.value='upstream_any_error'
  );

COMMIT;
