-- 迁移说明：0020_projects_applications 为 PRD-029 增加 Project/Application 归属层，并把归属快照接入 Key、日志、用量和预算。

-- 创建 projects 表：项目是当前租户内的一级业务归属，用于日志、用量和预算聚合。
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_code TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  owner_user_id TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'archived')),
  created_by TEXT,
  updated_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (owner_user_id) REFERENCES users(user_id),
  FOREIGN KEY (created_by) REFERENCES users(user_id),
  FOREIGN KEY (updated_by) REFERENCES users(user_id),
  UNIQUE (tenant_id, project_code)
);

-- 创建 applications 表：应用是项目下的二级业务归属，不跨项目独立存在。
CREATE TABLE IF NOT EXISTS applications (
  application_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  application_code TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'archived')),
  created_by TEXT,
  updated_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (project_id) REFERENCES projects(project_id),
  FOREIGN KEY (created_by) REFERENCES users(user_id),
  FOREIGN KEY (updated_by) REFERENCES users(user_id),
  UNIQUE (tenant_id, project_id, application_code)
);

-- 创建 project_events 表：保存项目域事件摘要，后续 Webhook 可按事件信封复用。
CREATE TABLE IF NOT EXISTS project_events (
  project_event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT,
  application_id TEXT,
  event_type TEXT NOT NULL CHECK (event_type IN ('project.created', 'application.created', 'virtual_key.bound')),
  actor_user_id TEXT,
  request_id TEXT NOT NULL,
  trace_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (project_id) REFERENCES projects(project_id),
  FOREIGN KEY (application_id) REFERENCES applications(application_id),
  FOREIGN KEY (actor_user_id) REFERENCES users(user_id)
);

-- 扩展 virtual_keys：Key 绑定项目和应用后，数据面可从鉴权结果恢复业务归属。
ALTER TABLE virtual_keys ADD COLUMN project_id TEXT;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE virtual_keys ADD COLUMN application_id TEXT;

-- 扩展 request_logs：日志保存请求发生时的项目/应用归属快照，历史数据不随后续改绑漂移。
ALTER TABLE request_logs ADD COLUMN project_id TEXT;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE request_logs ADD COLUMN application_id TEXT;

-- 扩展 usage_ledger：用量账本冗余项目/应用 ID，支撑成本聚合和预算判断。
ALTER TABLE usage_ledger ADD COLUMN project_id TEXT;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE usage_ledger ADD COLUMN application_id TEXT;

-- SQLite 无法直接修改 CHECK 约束，预算 scope 扩展需要重建三张预算表。
PRAGMA foreign_keys=off;

CREATE TABLE budget_rules_new (
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

-- 数据迁移说明：复制旧表数据到新结构，只迁移兼容字段，避免预算历史丢失。
INSERT INTO budget_rules_new(
  budget_rule_id, tenant_id, scope_type, scope_id, name, daily_budget_cny,
  warning_threshold_percent, exhausted_threshold_percent, exhausted_action, status,
  created_by, updated_by, created_at, updated_at
)
SELECT
  budget_rule_id, tenant_id, scope_type, scope_id, name, daily_budget_cny,
  warning_threshold_percent, exhausted_threshold_percent, exhausted_action, status,
  created_by, updated_by, created_at, updated_at
FROM budget_rules;

DROP TABLE budget_rules;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE budget_rules_new RENAME TO budget_rules;

CREATE TABLE budget_alert_events_new (
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
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules(budget_rule_id),
  UNIQUE (tenant_id, budget_rule_id, budget_date, threshold_percent)
);

-- 数据迁移说明：复制旧表数据到新结构，只迁移兼容字段，避免预算历史丢失。
INSERT INTO budget_alert_events_new(
  budget_alert_event_id, tenant_id, budget_rule_id, scope_type, scope_id,
  budget_date, threshold_percent, event_type, current_cost_cny,
  projected_cost_cny, budget_cny, action_taken, request_id, trace_id, created_at
)
SELECT
  budget_alert_event_id, tenant_id, budget_rule_id, scope_type, scope_id,
  budget_date, threshold_percent, event_type, current_cost_cny,
  projected_cost_cny, budget_cny, action_taken, request_id, trace_id, created_at
FROM budget_alert_events;

DROP TABLE budget_alert_events;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE budget_alert_events_new RENAME TO budget_alert_events;

CREATE TABLE budget_evaluations_new (
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
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules(budget_rule_id)
);

-- 数据迁移说明：复制旧表数据到新结构，只迁移兼容字段，避免预算历史丢失。
INSERT INTO budget_evaluations_new(
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
FROM budget_evaluations;

DROP TABLE budget_evaluations;
-- 字段变更说明：继续调整项目/应用归属字段，保证 Key、日志和用量账本的快照字段成对维护。
ALTER TABLE budget_evaluations_new RENAME TO budget_evaluations;

PRAGMA foreign_keys=on;

-- 创建项目/应用相关索引：保证控制台筛选、日志查询和用量聚合不退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_projects_tenant_status
  ON projects(tenant_id, status, updated_at DESC);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_projects_owner
  ON projects(tenant_id, owner_user_id);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_applications_tenant_project
  ON applications(tenant_id, project_id, status);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_project_events_tenant_time
  ON project_events(tenant_id, created_at DESC);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_project_events_type_time
  ON project_events(tenant_id, event_type, created_at DESC);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_virtual_keys_project
  ON virtual_keys(tenant_id, project_id, status);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_virtual_keys_application
  ON virtual_keys(tenant_id, application_id, status);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_project_time
  ON request_logs(tenant_id, project_id, created_at DESC);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_application_time
  ON request_logs(tenant_id, application_id, created_at DESC);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_usage_ledger_project_time
  ON usage_ledger(tenant_id, project_id, created_at DESC);
-- 索引创建说明：补建查询索引，保障控制台筛选、日志查询和预算评估不会退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_usage_ledger_application_time
  ON usage_ledger(tenant_id, application_id, created_at DESC);

-- 重建预算索引：预算表重命名会移除旧索引，必须重新创建所有查询依赖索引。
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
