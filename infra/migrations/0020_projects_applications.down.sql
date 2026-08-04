-- 回滚说明：撤销 PRD-029 Project/Application 归属层，保留旧 tenant/virtual_key 预算语义。

-- 删除新增索引，避免后续 DROP COLUMN 或 DROP TABLE 时仍被索引引用。
DROP INDEX IF EXISTS idx_budget_evaluations_tenant_date;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_budget_evaluations_request_id;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_budget_alert_events_request_id;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_budget_alert_events_tenant_date;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_budget_rules_status;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_budget_rules_tenant_scope;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_usage_ledger_application_time;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_usage_ledger_project_time;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_request_logs_application_time;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_request_logs_project_time;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_virtual_keys_application;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_virtual_keys_project;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_project_events_type_time;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_project_events_tenant_time;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_applications_tenant_project;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_projects_owner;
-- 回滚索引说明：删除该索引以解除后续表结构调整依赖。
DROP INDEX IF EXISTS idx_projects_tenant_status;

-- SQLite 修改 CHECK 约束需要重建预算表；回滚时只保留旧版本支持的 scope。
PRAGMA foreign_keys=off;

CREATE TABLE budget_rules_old (
  budget_rule_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'virtual_key')),
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

-- 数据迁移说明：复制旧表数据到新结构，只迁移兼容字段，避免预算历史丢失。
INSERT INTO budget_rules_old(
  budget_rule_id, tenant_id, scope_type, scope_id, name, daily_budget_cny,
  warning_threshold_percent, exhausted_threshold_percent, exhausted_action, status,
  created_by, updated_by, created_at, updated_at
)
SELECT
  budget_rule_id, tenant_id, scope_type, scope_id, name, daily_budget_cny,
  warning_threshold_percent, exhausted_threshold_percent, exhausted_action, status,
  created_by, updated_by, created_at, updated_at
FROM budget_rules
WHERE scope_type IN ('tenant', 'virtual_key');

DROP TABLE budget_rules;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE budget_rules_old RENAME TO budget_rules;

CREATE TABLE budget_alert_events_old (
  budget_alert_event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  budget_rule_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'virtual_key')),
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
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules(budget_rule_id),
  UNIQUE (tenant_id, budget_rule_id, budget_date, threshold_percent)
);

-- 数据迁移说明：复制旧表数据到新结构，只迁移兼容字段，避免预算历史丢失。
INSERT INTO budget_alert_events_old(
  budget_alert_event_id, tenant_id, budget_rule_id, scope_type, scope_id,
  budget_date, threshold_percent, event_type, current_cost_cny,
  projected_cost_cny, budget_cny, action_taken, request_id, trace_id, created_at
)
SELECT
  budget_alert_event_id, tenant_id, budget_rule_id, scope_type, scope_id,
  budget_date, threshold_percent, event_type, current_cost_cny,
  projected_cost_cny, budget_cny, action_taken, request_id, trace_id, created_at
FROM budget_alert_events
WHERE scope_type IN ('tenant', 'virtual_key');

DROP TABLE budget_alert_events;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE budget_alert_events_old RENAME TO budget_alert_events;

CREATE TABLE budget_evaluations_old (
  budget_evaluation_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  virtual_key_id TEXT,
  budget_rule_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'virtual_key')),
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
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules(budget_rule_id)
);

-- 数据迁移说明：复制旧表数据到新结构，只迁移兼容字段，避免预算历史丢失。
INSERT INTO budget_evaluations_old(
  budget_evaluation_id, tenant_id, request_id, trace_id, virtual_key_id,
  budget_rule_id, scope_type, scope_id, budget_date, spent_before_cny,
  estimated_request_cost_cny, projected_cost_cny, budget_cny,
  threshold_percent, budget_status, action_taken, reason_code, created_at
)
SELECT
  budget_evaluation_id, tenant_id, request_id, trace_id, virtual_key_id,
  budget_rule_id, scope_type, scope_id, budget_date, spent_before_cny,
  estimated_request_cost_cny, projected_cost_cny, budget_cny,
  threshold_percent, budget_status, action_taken, reason_code, created_at
FROM budget_evaluations
WHERE scope_type IN ('tenant', 'virtual_key');

DROP TABLE budget_evaluations;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE budget_evaluations_old RENAME TO budget_evaluations;

PRAGMA foreign_keys=on;

-- 移除新增列。当前项目要求 SQLite 版本支持 DROP COLUMN；失败时应通过纠正迁移处理。
ALTER TABLE virtual_keys DROP COLUMN project_id;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE virtual_keys DROP COLUMN application_id;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE request_logs DROP COLUMN project_id;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE request_logs DROP COLUMN application_id;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE usage_ledger DROP COLUMN project_id;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE usage_ledger DROP COLUMN application_id;

-- 删除项目域表，顺序按外键依赖从事件到应用再到项目。
DROP TABLE IF EXISTS project_events;
-- 表删除说明：旧表已经完成数据搬迁或属于回滚清理对象，删除前保持操作可追踪。
DROP TABLE IF EXISTS applications;
-- 表删除说明：旧表已经完成数据搬迁或属于回滚清理对象，删除前保持操作可追踪。
DROP TABLE IF EXISTS projects;

-- 恢复旧预算索引，保证回滚后的 PRD-023 查询性能与原始版本一致。
CREATE INDEX IF NOT EXISTS idx_budget_rules_tenant_scope
  ON budget_rules(tenant_id, scope_type, scope_id);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_budget_rules_status
  ON budget_rules(tenant_id, status);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_budget_alert_events_tenant_date
  ON budget_alert_events(tenant_id, budget_date DESC);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_budget_alert_events_request_id
  ON budget_alert_events(request_id);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_budget_evaluations_request_id
  ON budget_evaluations(tenant_id, request_id);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_budget_evaluations_tenant_date
  ON budget_evaluations(tenant_id, budget_date DESC);
