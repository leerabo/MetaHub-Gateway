-- 迁移说明：0015_budget_rules 为 PRD-023 增加租户和 Virtual Key 日预算、告警去重和预算命中解释。

-- 创建 budget_rules 表：保存租户级与 Virtual Key 级日预算配置，所有规则都按 tenant_id 隔离。
CREATE TABLE IF NOT EXISTS budget_rules (
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

-- 创建 budget_alert_events 表：记录每日阈值事件，并通过唯一约束避免 80%/100% 告警重复刷屏。
CREATE TABLE IF NOT EXISTS budget_alert_events (
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

-- 创建 budget_evaluations 表：保存预算命中请求的解释记录，便于从调用日志追踪预算拦截原因。
CREATE TABLE IF NOT EXISTS budget_evaluations (
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

-- 调整 request_logs 表：保存预算命中状态，便于调用详情直接解释预算阻断原因。
ALTER TABLE request_logs
  ADD COLUMN budget_status TEXT;

-- 调整 request_logs 表：保存命中的预算规则 ID，便于从调用日志跳转预算配置。
ALTER TABLE request_logs
  ADD COLUMN budget_rule_id TEXT;

-- 调整 request_logs 表：保存预算 scope 类型，避免只看规则 ID 时无法解释租户级还是 Key 级命中。
ALTER TABLE request_logs
  ADD COLUMN budget_scope_type TEXT;

-- 调整 request_logs 表：保存预算 scope ID，只记录租户 ID 或 Virtual Key ID，不记录任何明文密钥。
ALTER TABLE request_logs
  ADD COLUMN budget_scope_id TEXT;

-- 调整 request_logs 表：保存预算动作，区分 80% 告警放行、100% 仅告警和 100% 阻断。
ALTER TABLE request_logs
  ADD COLUMN budget_action TEXT;

-- 调整 request_logs 表：保存预算原因码，前端可直接展示安全解释和修复建议。
ALTER TABLE request_logs
  ADD COLUMN budget_reason_code TEXT;

-- 调整 request_logs 表：保存命中时的当前 CNY 成本摘要，不包含 Prompt 或响应内容。
ALTER TABLE request_logs
  ADD COLUMN budget_current_cost_cny NUMERIC;

-- 调整 request_logs 表：保存命中预算上限 CNY，便于排障时解释阈值来源。
ALTER TABLE request_logs
  ADD COLUMN budget_limit_cny NUMERIC;

-- 创建 idx_budget_rules_tenant_scope 索引：支撑控制面和数据面按租户、scope 快速读取预算规则。
CREATE INDEX IF NOT EXISTS idx_budget_rules_tenant_scope
  ON budget_rules(tenant_id, scope_type, scope_id);

-- 创建 idx_budget_rules_status 索引：支撑控制面筛选 active/disabled 预算规则。
CREATE INDEX IF NOT EXISTS idx_budget_rules_status
  ON budget_rules(tenant_id, status);

-- 创建 idx_budget_alert_events_tenant_date 索引：支撑控制台按日期查看预算告警事件。
CREATE INDEX IF NOT EXISTS idx_budget_alert_events_tenant_date
  ON budget_alert_events(tenant_id, budget_date DESC);

-- 创建 idx_budget_alert_events_request_id 索引：支撑从首次触发阈值的 request_id 反查预算事件。
CREATE INDEX IF NOT EXISTS idx_budget_alert_events_request_id
  ON budget_alert_events(request_id);

-- 创建 idx_budget_evaluations_request_id 索引：支撑调用日志详情按 request_id 读取预算命中解释。
CREATE INDEX IF NOT EXISTS idx_budget_evaluations_request_id
  ON budget_evaluations(tenant_id, request_id);

-- 创建 idx_budget_evaluations_tenant_date 索引：支撑预算页面按业务日查看命中记录。
CREATE INDEX IF NOT EXISTS idx_budget_evaluations_tenant_date
  ON budget_evaluations(tenant_id, budget_date DESC);
