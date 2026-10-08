ALTER TABLE budget_rules ADD COLUMN effective_from_utc TEXT;
DROP INDEX IF EXISTS uq_budget_rules_tenant_id_environment_id_scope_type_scope_id;
CREATE UNIQUE INDEX uq_budget_rules_tenant_id_environment_id_scope_type_scope_id_period
  ON budget_rules(tenant_id, environment_id, scope_type, scope_id, budget_period);
