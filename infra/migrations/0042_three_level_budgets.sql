-- PRD-052 三级预算：补齐预算 scope 归属防线、正式账本热路径索引和可持久化越限事实。
-- 本迁移只保存安全 ID、业务日和整数微元，不保存 Prompt、Response、Token 或 Key 明文。
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- 先确认 0041 及全部依赖对象完整，避免在结构漂移的数据库上创建半套预算约束。
CREATE TEMP TABLE prd052_prerequisite_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd052_prerequisite_guard
BEFORE INSERT ON prd052_prerequisite_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd052_prerequisite_missing');
END;
-- 汇总版本、依赖表、既有触发器和名称冲突，任一异常都通过稳定 guard 原子中止。
INSERT INTO prd052_prerequisite_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM schema_migrations
    WHERE version='0041' AND status='applied'
  )=1 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='table' AND name IN (
      'tenants', 'users', 'environments', 'projects', 'applications',
      'virtual_keys', 'budget_rules', 'budget_alert_events',
      'budget_evaluations', 'budget_blocks', 'budget_reservations',
      'finops_ledger_entries', 'webhook_endpoints', 'webhook_deliveries'
    )
  )=14 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND name IN (
      'trg_budget_blocks_owner_insert', 'trg_budget_blocks_owner_update',
      'trg_budget_reservations_owner_insert', 'trg_budget_reservations_owner_update',
      'trg_finops_ledger_immutable_update_prd051',
      'trg_finops_ledger_immutable_delete_prd051'
    )
  )=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_master WHERE name IN (
    'budget_scope_events',
    'idx_finops_budget_tenant_usage_prd052',
    'idx_finops_budget_project_usage_prd052',
    'idx_finops_budget_application_usage_prd052',
    'idx_finops_budget_key_usage_prd052'
  ));
DROP TRIGGER trg_prd052_prerequisite_guard;
DROP TABLE prd052_prerequisite_guard;

-- 0042 以前缺少完整 owner 触发器，因此先审计历史规则与子表；发现越权数据时在持久 DDL 前失败。
CREATE TEMP TABLE prd052_historical_owner_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd052_historical_owner_guard
BEFORE INSERT ON prd052_historical_owner_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd052_historical_owner_mismatch');
END;
-- 在新增防线前逐行核对 Rule 与四张既有子表，避免把历史越权关系合法化。
INSERT INTO prd052_historical_owner_guard(issue_count)
SELECT
  (SELECT COUNT(*)
   FROM budget_rules rule
   WHERE NOT EXISTS (
       SELECT 1 FROM environments environment
       WHERE environment.environment_id=rule.environment_id
         AND environment.tenant_id=rule.tenant_id
     )
     OR (rule.scope_type='tenant' AND rule.scope_id!=rule.tenant_id)
     OR (rule.scope_type='project' AND NOT EXISTS (
       SELECT 1 FROM projects project
       WHERE project.project_id=rule.scope_id AND project.tenant_id=rule.tenant_id
     ))
     OR (rule.scope_type='application' AND NOT EXISTS (
       SELECT 1 FROM applications application
       WHERE application.application_id=rule.scope_id
         AND application.tenant_id=rule.tenant_id
     ))
     OR (rule.scope_type='virtual_key' AND NOT EXISTS (
       SELECT 1 FROM virtual_keys key
       WHERE key.virtual_key_id=rule.scope_id
         AND key.tenant_id=rule.tenant_id
         AND key.environment_id=rule.environment_id
     ))
     OR (rule.created_by IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM users actor
       WHERE actor.user_id=rule.created_by AND actor.tenant_id=rule.tenant_id
     ))
     OR (rule.updated_by IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM users actor
       WHERE actor.user_id=rule.updated_by AND actor.tenant_id=rule.tenant_id
     )))
  + (SELECT COUNT(*)
     FROM budget_alert_events child
     LEFT JOIN budget_rules parent ON parent.budget_rule_id=child.budget_rule_id
     WHERE parent.budget_rule_id IS NULL
        OR child.tenant_id!=parent.tenant_id
        OR child.environment_id!=parent.environment_id
        OR child.scope_type!=parent.scope_type
        OR child.scope_id!=parent.scope_id)
  + (SELECT COUNT(*)
     FROM budget_evaluations child
     LEFT JOIN budget_rules parent ON parent.budget_rule_id=child.budget_rule_id
     WHERE parent.budget_rule_id IS NULL
        OR child.tenant_id!=parent.tenant_id
        OR child.environment_id!=parent.environment_id
        OR child.scope_type!=parent.scope_type
        OR child.scope_id!=parent.scope_id)
  + (SELECT COUNT(*)
     FROM budget_blocks child
     LEFT JOIN budget_rules parent ON parent.budget_rule_id=child.budget_rule_id
     WHERE parent.budget_rule_id IS NULL
        OR child.tenant_id!=parent.tenant_id
        OR child.environment_id!=parent.environment_id
        OR child.scope_type!=parent.scope_type
        OR child.scope_id!=parent.scope_id)
  + (SELECT COUNT(*)
     FROM budget_reservations child
     LEFT JOIN budget_rules parent ON parent.budget_rule_id=child.budget_rule_id
     WHERE parent.budget_rule_id IS NULL
        OR child.tenant_id!=parent.tenant_id
        OR child.environment_id!=parent.environment_id
        OR child.scope_type!=parent.scope_type
        OR child.scope_id!=parent.scope_id);
DROP TRIGGER trg_prd052_historical_owner_guard;
DROP TABLE prd052_historical_owner_guard;

-- canonical 越限事件按规则和上海业务日去重；金额统一使用整数微元，避免浮点比较漂移。
CREATE TABLE budget_scope_events (
  budget_scope_event_id TEXT PRIMARY KEY CHECK (
    length(budget_scope_event_id)=36
    AND substr(budget_scope_event_id, 1, 4)='bse_'
    AND substr(budget_scope_event_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  event_id TEXT NOT NULL UNIQUE CHECK (
    length(event_id)=38
    AND substr(event_id, 1, 6)='bsevt_'
    AND substr(event_id, 7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  budget_rule_id TEXT NOT NULL,
  budget_scope_id TEXT NOT NULL CHECK (
    length(budget_scope_id)=36
    AND substr(budget_scope_id, 1, 4)='bsc_'
    AND substr(budget_scope_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  scope_type TEXT NOT NULL CHECK (
    scope_type IN ('tenant', 'project', 'application', 'virtual_key')
  ),
  scope_id TEXT NOT NULL CHECK (length(scope_id) BETWEEN 1 AND 128),
  budget_date TEXT NOT NULL CHECK (
    length(budget_date)=10
    AND budget_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
  ),
  action_taken TEXT NOT NULL CHECK (action_taken IN ('allow', 'block')),
  posted_cost_micros INTEGER NOT NULL CHECK (
    typeof(posted_cost_micros)='integer'
    AND posted_cost_micros BETWEEN 0 AND 1000000000000000
  ),
  reserved_cost_micros INTEGER NOT NULL CHECK (
    typeof(reserved_cost_micros)='integer'
    AND reserved_cost_micros BETWEEN 0 AND 1000000000000000
  ),
  estimated_request_cost_micros INTEGER NOT NULL CHECK (
    typeof(estimated_request_cost_micros)='integer'
    AND estimated_request_cost_micros BETWEEN 0 AND 1000000000000000
  ),
  projected_cost_micros INTEGER NOT NULL CHECK (
    typeof(projected_cost_micros)='integer'
    AND projected_cost_micros BETWEEN 0 AND 1000000000000000
  ),
  budget_micros INTEGER NOT NULL CHECK (
    typeof(budget_micros)='integer'
    AND budget_micros BETWEEN 1 AND 1000000000000000
  ),
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128 AND instr(request_id, char(0))=0
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 128 AND instr(trace_id, char(0))=0
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules(budget_rule_id),
  UNIQUE (tenant_id, environment_id, budget_rule_id, budget_date),
  CHECK (
    projected_cost_micros
      = posted_cost_micros + reserved_cost_micros + estimated_request_cost_micros
  ),
  CHECK (projected_cost_micros>=budget_micros)
);

-- 控制面按租户、Environment 和业务日读取首次越限事实，不扫描其他租户事件。
CREATE INDEX idx_budget_scope_events_tenant_date_prd052
  ON budget_scope_events(
    tenant_id, environment_id, budget_date DESC, created_at DESC,
    budget_scope_event_id DESC
  );

-- 预算消费只读取已 posted 的正式 Usage Charge；四个部分索引分别覆盖兼容 scope 链。
CREATE INDEX idx_finops_budget_tenant_usage_prd052
  ON finops_ledger_entries(
    tenant_id, environment_id, created_at, ledger_sequence
  )
  WHERE entry_type='usage_charge' AND direction='debit' AND status='posted';
CREATE INDEX idx_finops_budget_project_usage_prd052
  ON finops_ledger_entries(
    tenant_id, environment_id, project_id, created_at, ledger_sequence
  )
  WHERE entry_type='usage_charge' AND direction='debit' AND status='posted';
CREATE INDEX idx_finops_budget_application_usage_prd052
  ON finops_ledger_entries(
    tenant_id, environment_id, application_id, created_at, ledger_sequence
  )
  WHERE entry_type='usage_charge' AND direction='debit' AND status='posted';
CREATE INDEX idx_finops_budget_key_usage_prd052
  ON finops_ledger_entries(
    tenant_id, environment_id, virtual_key_id, created_at, ledger_sequence
  )
  WHERE entry_type='usage_charge' AND direction='debit' AND status='posted';

-- Rule 的 Environment、scope 与操作者都必须来自同一 tenant；tenant scope 不能伪造其他 scope_id。
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

-- Legacy 告警事件必须完整复制父 Rule 的 tenant/environment/scope 快照。
CREATE TRIGGER trg_budget_alert_events_prd052_owner_insert
BEFORE INSERT ON budget_alert_events
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id
    AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_alert_event_owner_scope_mismatch');
END;
CREATE TRIGGER trg_budget_alert_events_prd052_owner_update
BEFORE UPDATE OF tenant_id, environment_id, budget_rule_id, scope_type, scope_id
ON budget_alert_events
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id
    AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_alert_event_owner_scope_mismatch');
END;

-- Evaluation 是请求诊断事实，冗余 scope 同样不能脱离父 Rule。
CREATE TRIGGER trg_budget_evaluations_prd052_owner_insert
BEFORE INSERT ON budget_evaluations
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id
    AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_evaluation_owner_scope_mismatch');
END;
CREATE TRIGGER trg_budget_evaluations_prd052_owner_update
BEFORE UPDATE OF tenant_id, environment_id, budget_rule_id, scope_type, scope_id
ON budget_evaluations
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id
    AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_evaluation_owner_scope_mismatch');
END;

-- Block 可更新生命周期字段，但归属和 scope 每次写入都必须与父 Rule 一致。
CREATE TRIGGER trg_budget_blocks_prd052_scope_insert
BEFORE INSERT ON budget_blocks
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id
    AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_block_owner_scope_mismatch');
END;
CREATE TRIGGER trg_budget_blocks_prd052_scope_update
BEFORE UPDATE OF tenant_id, environment_id, budget_rule_id, scope_type, scope_id
ON budget_blocks
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id
    AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_block_owner_scope_mismatch');
END;

-- Reservation 是并发额度占用事实；不能用同租户的另一条 Rule 伪造 scope。
CREATE TRIGGER trg_budget_reservations_prd052_scope_insert
BEFORE INSERT ON budget_reservations
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id
    AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_reservation_owner_scope_mismatch');
END;
CREATE TRIGGER trg_budget_reservations_prd052_scope_update
BEFORE UPDATE OF tenant_id, environment_id, budget_rule_id, scope_type, scope_id
ON budget_reservations
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id
    AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_reservation_owner_scope_mismatch');
END;

-- canonical scope event 的父 Rule 快照必须完整一致，不能只依赖单列外键证明 ID 存在。
CREATE TRIGGER trg_budget_scope_events_prd052_owner_insert
BEFORE INSERT ON budget_scope_events
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id
    AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_scope_event_owner_scope_mismatch');
END;

-- 越限事实一旦提交即为 Webhook 的稳定来源；任何 UPDATE/DELETE 都必须失败。
CREATE TRIGGER trg_budget_scope_events_prd052_immutable_update
BEFORE UPDATE ON budget_scope_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'budget_scope_event_immutable');
END;
CREATE TRIGGER trg_budget_scope_events_prd052_immutable_delete
BEFORE DELETE ON budget_scope_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'budget_scope_event_immutable');
END;

COMMIT;
