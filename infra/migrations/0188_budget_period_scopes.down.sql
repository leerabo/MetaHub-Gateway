-- Revert only when every scope has one remaining period; never discard finance rules.
PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE budget_period_rollback_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER budget_period_rollback_abort
BEFORE INSERT ON budget_period_rollback_guard FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'budget_period_rules_must_be_resolved_before_rollback'); END;
INSERT INTO budget_period_rollback_guard(issue_count)
SELECT COUNT(*) FROM (
  SELECT tenant_id,environment_id,scope_type,scope_id
  FROM budget_rules GROUP BY tenant_id,environment_id,scope_type,scope_id
  HAVING COUNT(*)>1
);
DROP TRIGGER budget_period_rollback_abort;
DROP TABLE budget_period_rollback_guard;
CREATE TABLE budget_rules_period_old (
  budget_rule_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'virtual_key', 'project', 'application')),
  scope_id TEXT NOT NULL,
  name TEXT NOT NULL,
  daily_budget_cny NUMERIC NOT NULL CHECK (daily_budget_cny > 0),
  warning_threshold_percent INTEGER NOT NULL DEFAULT 80 CHECK (warning_threshold_percent > 0 AND warning_threshold_percent < 100),
  exhausted_threshold_percent INTEGER NOT NULL DEFAULT 100 CHECK (exhausted_threshold_percent = 100),
  exhausted_action TEXT NOT NULL DEFAULT 'alert_only' CHECK (exhausted_action IN ('alert_only', 'block')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_by TEXT,
  updated_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  budget_period TEXT NOT NULL DEFAULT 'day' CHECK (budget_period IN ('day', 'month', 'year')),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY (created_by) REFERENCES users(user_id),
  FOREIGN KEY (updated_by) REFERENCES users(user_id),
  UNIQUE (tenant_id, environment_id, scope_type, scope_id)
);
INSERT INTO budget_rules_period_old(
  budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,daily_budget_cny,
  warning_threshold_percent,exhausted_threshold_percent,exhausted_action,status,
  created_by,updated_by,created_at,updated_at,budget_period
)
SELECT budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,daily_budget_cny,
  warning_threshold_percent,exhausted_threshold_percent,exhausted_action,status,
  created_by,updated_by,created_at,updated_at,budget_period FROM budget_rules;
DROP TABLE budget_rules;
ALTER TABLE budget_rules_period_old RENAME TO budget_rules;
CREATE INDEX idx_budget_rules_tenant_environment_scope
  ON budget_rules(tenant_id,environment_id,scope_type,scope_id);
CREATE INDEX idx_budget_rules_environment_status
  ON budget_rules(tenant_id,environment_id,status);
CREATE UNIQUE INDEX uq_budget_rules_tenant_rule_prd074
  ON budget_rules(tenant_id,budget_rule_id);
CREATE TRIGGER trg_budget_rules_prd052_owner_insert
BEFORE INSERT ON budget_rules
FOR EACH ROW
WHEN NOT EXISTS (
      SELECT 1 FROM environments environment
      WHERE environment.environment_id=NEW.environment_id
        AND environment.tenant_id=NEW.tenant_id
    )
  OR (NEW.scope_type='tenant' AND NEW.scope_id!=NEW.tenant_id)
  OR (NEW.scope_type='project' AND NOT EXISTS (
      SELECT 1 FROM projects project
      WHERE project.project_id=NEW.scope_id AND project.tenant_id=NEW.tenant_id
    ))
  OR (NEW.scope_type='application' AND NOT EXISTS (
      SELECT 1 FROM applications application
      WHERE application.application_id=NEW.scope_id
        AND application.tenant_id=NEW.tenant_id
    ))
  OR (NEW.scope_type='virtual_key' AND NOT EXISTS (
      SELECT 1 FROM virtual_keys key
      WHERE key.virtual_key_id=NEW.scope_id
        AND key.tenant_id=NEW.tenant_id
        AND key.environment_id=NEW.environment_id
    ))
  OR (NEW.created_by IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM users actor
      WHERE actor.user_id=NEW.created_by AND actor.tenant_id=NEW.tenant_id
    ))
  OR (NEW.updated_by IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM users actor
      WHERE actor.user_id=NEW.updated_by AND actor.tenant_id=NEW.tenant_id
    ))
BEGIN
  SELECT RAISE(ABORT, 'budget_rule_owner_scope_mismatch');
END;

-- 归属更新既校验新 owner，也拒绝把已有历史子记录留在旧 tenant/environment/scope 下。
CREATE TRIGGER trg_budget_rules_prd052_owner_update
BEFORE UPDATE OF tenant_id, environment_id, scope_type, scope_id, created_by, updated_by
ON budget_rules
FOR EACH ROW
WHEN NOT EXISTS (
      SELECT 1 FROM environments environment
      WHERE environment.environment_id=NEW.environment_id
        AND environment.tenant_id=NEW.tenant_id
    )
  OR (NEW.scope_type='tenant' AND NEW.scope_id!=NEW.tenant_id)
  OR (NEW.scope_type='project' AND NOT EXISTS (
      SELECT 1 FROM projects project
      WHERE project.project_id=NEW.scope_id AND project.tenant_id=NEW.tenant_id
    ))
  OR (NEW.scope_type='application' AND NOT EXISTS (
      SELECT 1 FROM applications application
      WHERE application.application_id=NEW.scope_id
        AND application.tenant_id=NEW.tenant_id
    ))
  OR (NEW.scope_type='virtual_key' AND NOT EXISTS (
      SELECT 1 FROM virtual_keys key
      WHERE key.virtual_key_id=NEW.scope_id
        AND key.tenant_id=NEW.tenant_id
        AND key.environment_id=NEW.environment_id
    ))
  OR (NEW.created_by IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM users actor
      WHERE actor.user_id=NEW.created_by AND actor.tenant_id=NEW.tenant_id
    ))
  OR (NEW.updated_by IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM users actor
      WHERE actor.user_id=NEW.updated_by AND actor.tenant_id=NEW.tenant_id
    ))
  OR EXISTS (
      SELECT 1 FROM budget_alert_events child
      WHERE child.budget_rule_id=OLD.budget_rule_id
        AND (child.tenant_id!=NEW.tenant_id
          OR child.environment_id!=NEW.environment_id
          OR child.scope_type!=NEW.scope_type OR child.scope_id!=NEW.scope_id)
    )
  OR EXISTS (
      SELECT 1 FROM budget_evaluations child
      WHERE child.budget_rule_id=OLD.budget_rule_id
        AND (child.tenant_id!=NEW.tenant_id
          OR child.environment_id!=NEW.environment_id
          OR child.scope_type!=NEW.scope_type OR child.scope_id!=NEW.scope_id)
    )
  OR EXISTS (
      SELECT 1 FROM budget_blocks child
      WHERE child.budget_rule_id=OLD.budget_rule_id
        AND (child.tenant_id!=NEW.tenant_id
          OR child.environment_id!=NEW.environment_id
          OR child.scope_type!=NEW.scope_type OR child.scope_id!=NEW.scope_id)
    )
  OR EXISTS (
      SELECT 1 FROM budget_reservations child
      WHERE child.budget_rule_id=OLD.budget_rule_id
        AND (child.tenant_id!=NEW.tenant_id
          OR child.environment_id!=NEW.environment_id
          OR child.scope_type!=NEW.scope_type OR child.scope_id!=NEW.scope_id)
    )
  OR EXISTS (
      SELECT 1 FROM budget_scope_events child
      WHERE child.budget_rule_id=OLD.budget_rule_id
        AND (child.tenant_id!=NEW.tenant_id
          OR child.environment_id!=NEW.environment_id
          OR child.scope_type!=NEW.scope_type OR child.scope_id!=NEW.scope_id)
    )
BEGIN
  SELECT RAISE(ABORT, 'budget_rule_owner_scope_mismatch');
END;

-- Rule ID 是子表和 Webhook 事实的稳定引用，不能通过 UPDATE 改写身份。
CREATE TRIGGER trg_budget_rules_prd052_identity_update
BEFORE UPDATE OF budget_rule_id ON budget_rules
FOR EACH ROW
WHEN NEW.budget_rule_id IS NOT OLD.budget_rule_id
BEGIN
  SELECT RAISE(ABORT, 'budget_rule_identity_immutable');
END;

COMMIT;
PRAGMA legacy_alter_table=OFF;
PRAGMA foreign_keys=ON;
