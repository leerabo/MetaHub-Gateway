-- 回滚说明：移除 PRD-030 Environment 对象和环境外键，恢复 PRD-029 的字符串环境与旧预算唯一约束。

-- SQLite 表重建前关闭外键检查，结束后重新开启。
PRAGMA foreign_keys=off;

-- 显式事务保证回滚不会留下部分删除的环境字段。
BEGIN;

-- 删除 PRD-030 新增索引，避免删除字段或表时保留无效引用。
DROP INDEX IF EXISTS idx_environments_tenant_status;

-- 索引删除说明：环境事件表将在本回滚末尾删除。
DROP INDEX IF EXISTS idx_environment_events_tenant_time;

-- 索引删除说明：Key 将恢复使用旧 environment 代码索引。
DROP INDEX IF EXISTS idx_virtual_keys_environment_id;

-- 索引删除说明：模型别名恢复旧 `(tenant_id, alias, environment)` 唯一约束。
DROP INDEX IF EXISTS idx_model_aliases_environment_alias;

-- 索引删除说明：路由决策恢复按旧环境代码查询。
DROP INDEX IF EXISTS idx_routing_decisions_environment_time;

-- 索引删除说明：调用日志不再保留环境主数据字段。
DROP INDEX IF EXISTS idx_request_logs_environment_time;

-- 索引删除说明：用量账本不再保留环境主数据字段。
DROP INDEX IF EXISTS idx_usage_ledger_environment_time;

-- 索引删除说明：预算表将重建为旧唯一约束。
DROP INDEX IF EXISTS idx_budget_rules_tenant_environment_scope;

-- 索引删除说明：预算状态查询恢复旧索引。
DROP INDEX IF EXISTS idx_budget_rules_environment_status;

-- 索引删除说明：预算命中记录将移除 Environment 维度。
DROP INDEX IF EXISTS idx_budget_evaluations_environment_date;

-- 创建旧结构预算规则表：多环境出现同一 scope 时保留更新时间最新的一条。
CREATE TABLE budget_rules_legacy_new (
  budget_rule_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
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
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (created_by) REFERENCES users(user_id),
  FOREIGN KEY (updated_by) REFERENCES users(user_id),
  UNIQUE (tenant_id, scope_type, scope_id)
);

-- 数据回滚说明：按更新时间和 ID 稳定选择每个旧 scope 的最终规则，避免旧唯一约束冲突。
INSERT INTO budget_rules_legacy_new(
  budget_rule_id, tenant_id, scope_type, scope_id, name, daily_budget_cny,
  warning_threshold_percent, exhausted_threshold_percent, exhausted_action,
  status, created_by, updated_by, created_at, updated_at
)
SELECT
  br.budget_rule_id, br.tenant_id, br.scope_type, br.scope_id, br.name,
  br.daily_budget_cny, br.warning_threshold_percent, br.exhausted_threshold_percent,
  br.exhausted_action, br.status, br.created_by, br.updated_by, br.created_at, br.updated_at
FROM budget_rules br
WHERE br.budget_rule_id=(
  SELECT newer.budget_rule_id
  FROM budget_rules newer
  WHERE newer.tenant_id=br.tenant_id
    AND newer.scope_type=br.scope_type
    AND newer.scope_id=br.scope_id
  ORDER BY newer.updated_at DESC, newer.budget_rule_id DESC
  LIMIT 1
);

-- 创建旧结构预算告警事件表：仅保留回滚后仍存在规则的事件。
CREATE TABLE budget_alert_events_legacy_new (
  budget_alert_event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  budget_rule_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'virtual_key', 'project', 'application')),
  scope_id TEXT NOT NULL,
  budget_date TEXT NOT NULL,
  threshold_percent INTEGER NOT NULL CHECK (threshold_percent > 0),
  event_type TEXT NOT NULL CHECK (event_type IN ('budget.threshold_reached', 'budget.exhausted')),
  current_cost_cny NUMERIC NOT NULL DEFAULT 0 CHECK (current_cost_cny >= 0),
  projected_cost_cny NUMERIC NOT NULL DEFAULT 0 CHECK (projected_cost_cny >= 0),
  budget_cny NUMERIC NOT NULL CHECK (budget_cny > 0),
  action_taken TEXT NOT NULL CHECK (action_taken IN ('warn', 'allow', 'block')),
  request_id TEXT,
  trace_id TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules_legacy_new(budget_rule_id),
  UNIQUE (tenant_id, budget_rule_id, budget_date, threshold_percent)
);

-- 数据回滚说明：过滤已被多环境去重淘汰规则的告警历史，保持外键可用。
INSERT INTO budget_alert_events_legacy_new(
  budget_alert_event_id, tenant_id, budget_rule_id, scope_type, scope_id,
  budget_date, threshold_percent, event_type, current_cost_cny,
  projected_cost_cny, budget_cny, action_taken, request_id, trace_id, created_at
)
SELECT
  bae.budget_alert_event_id, bae.tenant_id, bae.budget_rule_id, bae.scope_type,
  bae.scope_id, bae.budget_date, bae.threshold_percent, bae.event_type,
  bae.current_cost_cny, bae.projected_cost_cny, bae.budget_cny,
  bae.action_taken, bae.request_id, bae.trace_id, bae.created_at
FROM budget_alert_events bae
JOIN budget_rules_legacy_new br ON br.budget_rule_id=bae.budget_rule_id;

-- 创建旧结构预算命中表：仅保留回滚后仍存在规则的命中记录。
CREATE TABLE budget_evaluations_legacy_new (
  budget_evaluation_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  virtual_key_id TEXT,
  budget_rule_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'virtual_key', 'project', 'application')),
  scope_id TEXT NOT NULL,
  budget_date TEXT NOT NULL,
  spent_before_cny NUMERIC NOT NULL DEFAULT 0 CHECK (spent_before_cny >= 0),
  estimated_request_cost_cny NUMERIC NOT NULL DEFAULT 0 CHECK (estimated_request_cost_cny >= 0),
  projected_cost_cny NUMERIC NOT NULL DEFAULT 0 CHECK (projected_cost_cny >= 0),
  budget_cny NUMERIC NOT NULL CHECK (budget_cny > 0),
  threshold_percent INTEGER NOT NULL CHECK (threshold_percent > 0),
  budget_status TEXT NOT NULL CHECK (budget_status IN ('threshold_reached', 'exhausted')),
  action_taken TEXT NOT NULL CHECK (action_taken IN ('warn', 'allow', 'block')),
  reason_code TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules_legacy_new(budget_rule_id)
);

-- 数据回滚说明：过滤已被多环境去重淘汰规则的预算命中历史。
INSERT INTO budget_evaluations_legacy_new(
  budget_evaluation_id, tenant_id, request_id, trace_id, virtual_key_id,
  budget_rule_id, scope_type, scope_id, budget_date, spent_before_cny,
  estimated_request_cost_cny, projected_cost_cny, budget_cny,
  threshold_percent, budget_status, action_taken, reason_code, created_at
)
SELECT
  be.budget_evaluation_id, be.tenant_id, be.request_id, be.trace_id,
  be.virtual_key_id, be.budget_rule_id, be.scope_type, be.scope_id,
  be.budget_date, be.spent_before_cny, be.estimated_request_cost_cny,
  be.projected_cost_cny, be.budget_cny, be.threshold_percent,
  be.budget_status, be.action_taken, be.reason_code, be.created_at
FROM budget_evaluations be
JOIN budget_rules_legacy_new br ON br.budget_rule_id=be.budget_rule_id;

-- 删除包含环境维度的预算子表，数据已复制到旧结构。
DROP TABLE budget_alert_events;

-- 表删除说明：预算命中记录已复制到旧结构。
DROP TABLE budget_evaluations;

-- 表删除说明：多环境预算规则已按旧 scope 唯一约束完成去重复制。
DROP TABLE budget_rules;

-- 表名恢复说明：把去除环境字段后的预算规则表恢复成业务稳定表名。
ALTER TABLE budget_rules_legacy_new RENAME TO budget_rules;

-- 恢复预算告警事件稳定表名。
ALTER TABLE budget_alert_events_legacy_new RENAME TO budget_alert_events;

-- 恢复预算命中记录稳定表名。
ALTER TABLE budget_evaluations_legacy_new RENAME TO budget_evaluations;

-- 删除 routing_decisions 的 Environment 外键字段，旧 environment 代码继续保留。
ALTER TABLE routing_decisions DROP COLUMN environment_id;

-- 删除 request_logs 环境主数据字段，恢复 PRD-029 日志结构。
ALTER TABLE request_logs DROP COLUMN environment_id;

-- 字段删除说明：移除 PRD-030 新增的环境代码快照。
ALTER TABLE request_logs DROP COLUMN environment_code;

-- 删除 usage_ledger 环境主数据字段，恢复 PRD-029 用量结构。
ALTER TABLE usage_ledger DROP COLUMN environment_id;

-- 字段删除说明：移除 PRD-030 新增的环境代码快照。
ALTER TABLE usage_ledger DROP COLUMN environment_code;

-- 删除 model_aliases 环境主数据字段，旧环境代码和唯一约束继续生效。
ALTER TABLE model_aliases DROP COLUMN environment_id;

-- 删除 virtual_keys 环境主数据字段，旧 environment 字段继续作为权威值。
ALTER TABLE virtual_keys DROP COLUMN environment_id;

-- 删除环境事件表：回滚后不再存在 Environment 业务对象。
DROP TABLE IF EXISTS environment_events;

-- 删除租户默认环境触发器：回滚后 tenants 不再依赖 Environment 表。
DROP TRIGGER IF EXISTS trg_tenants_default_environment;

-- 删除环境主表：所有关联字段已经移除。
DROP TABLE IF EXISTS environments;

-- 索引恢复说明：恢复 PRD-029 按租户和业务范围查找预算规则的索引。
CREATE INDEX IF NOT EXISTS idx_budget_rules_tenant_scope
  ON budget_rules(tenant_id, scope_type, scope_id);

-- 索引恢复说明：恢复 PRD-029 按租户和启停状态筛选预算规则的索引。
CREATE INDEX IF NOT EXISTS idx_budget_rules_status
  ON budget_rules(tenant_id, status);

-- 恢复预算告警事件查询索引。
CREATE INDEX IF NOT EXISTS idx_budget_alert_events_tenant_date
  ON budget_alert_events(tenant_id, budget_date DESC);

-- 索引创建说明：恢复从请求反查预算告警事件的能力。
CREATE INDEX IF NOT EXISTS idx_budget_alert_events_request_id
  ON budget_alert_events(request_id);

-- 索引恢复说明：恢复按租户和请求标识反查预算命中记录的索引。
CREATE INDEX IF NOT EXISTS idx_budget_evaluations_request_id
  ON budget_evaluations(tenant_id, request_id);

-- 索引创建说明：恢复按租户和业务日查看预算命中的能力。
CREATE INDEX IF NOT EXISTS idx_budget_evaluations_tenant_date
  ON budget_evaluations(tenant_id, budget_date DESC);

-- 提交全部回滚操作。
COMMIT;

-- 回滚完成后恢复外键检查。
PRAGMA foreign_keys=on;
