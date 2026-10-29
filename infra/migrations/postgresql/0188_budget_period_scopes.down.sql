DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM budget_rules
    GROUP BY tenant_id, environment_id, scope_type, scope_id
    HAVING COUNT(*) > 1
  ) THEN
    RAISE EXCEPTION 'budget_period_rules_must_be_resolved_before_rollback';
  END IF;
END $$;
DROP INDEX IF EXISTS uq_budget_rules_tenant_id_environment_id_scope_type_scope_id_period;
CREATE UNIQUE INDEX uq_budget_rules_tenant_id_environment_id_scope_type_scope_id
  ON budget_rules(tenant_id, environment_id, scope_type, scope_id);
ALTER TABLE budget_rules DROP COLUMN effective_from_utc;
