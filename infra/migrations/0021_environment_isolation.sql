-- 迁移说明：0021_environment_isolation 为 PRD-030 建立租户级 Environment 主数据，并把环境身份贯穿 Key、别名、预算、路由、日志与用量。

-- SQLite 的表重建需要在事务开始前关闭外键检查；迁移结束后会重新开启并由测试执行 foreign_key_check。
PRAGMA foreign_keys=off;

-- 显式事务保证多表回填和预算表重建要么全部成功，要么全部回滚。
BEGIN;

-- 创建 environments 表：环境是租户内 dev/test/prod 配置边界，不代表部署集群或地域。
CREATE TABLE IF NOT EXISTS environments (
  environment_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL CHECK (code IN ('dev', 'test', 'prod')),
  name TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_by TEXT,
  updated_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (created_by) REFERENCES users(user_id),
  FOREIGN KEY (updated_by) REFERENCES users(user_id),
  UNIQUE (tenant_id, code)
);

-- 创建 environment_events 表：保存环境创建和更新的安全事件摘要，供审计与 Webhook 信封复用。
CREATE TABLE IF NOT EXISTS environment_events (
  environment_event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('environment.created', 'environment.updated')),
  actor_user_id TEXT,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY (actor_user_id) REFERENCES users(user_id)
);

-- 回填 Environment 主数据：每个租户至少有 dev，历史 Key、别名或路由中出现的 test/prod 也同步建档。
WITH tenant_environment_codes AS (
  SELECT tenant_id, 'dev' AS code FROM tenants
  UNION
  SELECT tenant_id, environment AS code FROM virtual_keys WHERE environment IN ('dev', 'test', 'prod')
  UNION
  SELECT tenant_id, environment AS code FROM model_aliases WHERE environment IN ('dev', 'test', 'prod')
  UNION
  SELECT tenant_id, environment AS code FROM routing_decisions WHERE environment IN ('dev', 'test', 'prod')
)
-- 历史代码回填说明：为每个租户和有效环境代码创建唯一环境主数据记录。
INSERT INTO environments(
  environment_id, tenant_id, code, name, description, status,
  created_by, updated_by, created_at, updated_at
)
SELECT
  'env_' || lower(hex(randomblob(16))),
  tenant_id,
  code,
  CASE code WHEN 'dev' THEN '开发环境' WHEN 'test' THEN '测试环境' ELSE '生产环境' END,
  '由 PRD-030 数据迁移根据历史环境代码自动创建。',
  'active',
  NULL,
  NULL,
  strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
  strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
FROM tenant_environment_codes;

-- 创建租户默认环境触发器：迁移后新增或种子插入的租户自动获得 dev Environment。
CREATE TRIGGER IF NOT EXISTS trg_tenants_default_environment
AFTER INSERT ON tenants
FOR EACH ROW
BEGIN
  INSERT OR IGNORE INTO environments(
    environment_id, tenant_id, code, name, description, status,
    created_by, updated_by, created_at, updated_at
  )
  VALUES(
    'env_' || lower(hex(randomblob(16))),
    NEW.tenant_id,
    'dev',
    '开发环境',
    '租户创建时自动生成的默认开发环境。',
    'active',
    NULL,
    NULL,
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
  );
END;

-- 扩展 virtual_keys：environment_id 是权威关联，旧 environment 字段继续作为兼容代码快照。
ALTER TABLE virtual_keys ADD COLUMN environment_id TEXT REFERENCES environments(environment_id);

-- 回填 Key 环境关联：同时按 tenant_id 和不可变 code 匹配，禁止跨租户绑定。
UPDATE virtual_keys
SET environment_id=(
  SELECT e.environment_id
  FROM environments e
  WHERE e.tenant_id=virtual_keys.tenant_id AND e.code=virtual_keys.environment
);

-- 扩展 model_aliases：同名别名后续按环境 ID 解析，代码字段保留兼容。
ALTER TABLE model_aliases ADD COLUMN environment_id TEXT REFERENCES environments(environment_id);

-- 回填模型别名环境关联：环境 ID 与旧代码一一对应。
UPDATE model_aliases
SET environment_id=(
  SELECT e.environment_id
  FROM environments e
  WHERE e.tenant_id=model_aliases.tenant_id AND e.code=model_aliases.environment
);

-- 扩展 routing_decisions：成功和失败路由都保存环境主数据 ID。
ALTER TABLE routing_decisions ADD COLUMN environment_id TEXT REFERENCES environments(environment_id);

-- 回填历史路由环境：使用记录自身 tenant_id 和环境代码，避免依赖可能已改绑的 Key。
UPDATE routing_decisions
SET environment_id=(
  SELECT e.environment_id
  FROM environments e
  WHERE e.tenant_id=routing_decisions.tenant_id AND e.code=routing_decisions.environment
);

-- 扩展 request_logs：保存环境 ID 和不可变代码快照，失败请求也可以按环境检索。
ALTER TABLE request_logs ADD COLUMN environment_id TEXT REFERENCES environments(environment_id);

-- 字段变更说明：environment_code 是请求发生时的代码快照，不随后续展示名称变化。
ALTER TABLE request_logs ADD COLUMN environment_code TEXT;

-- 回填调用日志：优先采用关联 Key 环境，无法反查的历史记录安全回落到租户 dev。
UPDATE request_logs
SET environment_id=COALESCE(
      (SELECT vk.environment_id FROM virtual_keys vk
       WHERE vk.tenant_id=request_logs.tenant_id AND vk.virtual_key_id=request_logs.virtual_key_id),
      (SELECT e.environment_id FROM environments e
       WHERE e.tenant_id=request_logs.tenant_id AND e.code='dev')
    ),
    environment_code=COALESCE(
      (SELECT vk.environment FROM virtual_keys vk
       WHERE vk.tenant_id=request_logs.tenant_id AND vk.virtual_key_id=request_logs.virtual_key_id),
      'dev'
    );

-- 扩展 usage_ledger：环境维度进入成本聚合和预算消费查询。
ALTER TABLE usage_ledger ADD COLUMN environment_id TEXT REFERENCES environments(environment_id);

-- 字段变更说明：environment_code 保存账本发生时的稳定代码快照。
ALTER TABLE usage_ledger ADD COLUMN environment_code TEXT;

-- 回填用量账本：优先沿用 request_logs 快照，缺失时按租户 dev 兜底。
UPDATE usage_ledger
SET environment_id=COALESCE(
      (SELECT rl.environment_id FROM request_logs rl
       WHERE rl.tenant_id=usage_ledger.tenant_id AND rl.request_id=usage_ledger.request_id),
      (SELECT e.environment_id FROM environments e
       WHERE e.tenant_id=usage_ledger.tenant_id AND e.code='dev')
    ),
    environment_code=COALESCE(
      (SELECT rl.environment_code FROM request_logs rl
       WHERE rl.tenant_id=usage_ledger.tenant_id AND rl.request_id=usage_ledger.request_id),
      'dev'
    );

-- 重建 budget_rules：旧唯一约束不含环境，无法为同一 scope 分别配置 test/prod 预算。
CREATE TABLE budget_rules_environment_new (
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
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY (created_by) REFERENCES users(user_id),
  FOREIGN KEY (updated_by) REFERENCES users(user_id),
  UNIQUE (tenant_id, environment_id, scope_type, scope_id)
);

-- 数据迁移说明：旧预算全部归入租户 dev，保持 PRD-023 的默认环境语义。
INSERT INTO budget_rules_environment_new(
  budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
  daily_budget_cny, warning_threshold_percent, exhausted_threshold_percent,
  exhausted_action, status, created_by, updated_by, created_at, updated_at
)
SELECT
  br.budget_rule_id,
  br.tenant_id,
  e.environment_id,
  br.scope_type,
  br.scope_id,
  br.name,
  br.daily_budget_cny,
  br.warning_threshold_percent,
  br.exhausted_threshold_percent,
  br.exhausted_action,
  br.status,
  br.created_by,
  br.updated_by,
  br.created_at,
  br.updated_at
FROM budget_rules br
JOIN environments e ON e.tenant_id=br.tenant_id AND e.code='dev';

-- 重建 budget_alert_events：阈值事件冗余 environment_id，避免同 scope 跨环境解释错误。
CREATE TABLE budget_alert_events_environment_new (
  budget_alert_event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
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
  FOREIGN KEY (environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules_environment_new(budget_rule_id),
  UNIQUE (tenant_id, budget_rule_id, budget_date, threshold_percent)
);

-- 数据迁移说明：历史预算事件沿用所属预算规则回填后的 dev Environment。
INSERT INTO budget_alert_events_environment_new(
  budget_alert_event_id, tenant_id, environment_id, budget_rule_id, scope_type,
  scope_id, budget_date, threshold_percent, event_type, current_cost_cny,
  projected_cost_cny, budget_cny, action_taken, request_id, trace_id, created_at
)
SELECT
  bae.budget_alert_event_id,
  bae.tenant_id,
  br.environment_id,
  bae.budget_rule_id,
  bae.scope_type,
  bae.scope_id,
  bae.budget_date,
  bae.threshold_percent,
  bae.event_type,
  bae.current_cost_cny,
  bae.projected_cost_cny,
  bae.budget_cny,
  bae.action_taken,
  bae.request_id,
  bae.trace_id,
  bae.created_at
FROM budget_alert_events bae
JOIN budget_rules_environment_new br ON br.budget_rule_id=bae.budget_rule_id;

-- 重建 budget_evaluations：每次预算命中保留 Environment，便于调用日志详情解释。
CREATE TABLE budget_evaluations_environment_new (
  budget_evaluation_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
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
  FOREIGN KEY (environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules_environment_new(budget_rule_id)
);

-- 数据迁移说明：历史命中记录通过预算规则得到环境 ID，不根据当前 Key 反推以免改绑漂移。
INSERT INTO budget_evaluations_environment_new(
  budget_evaluation_id, tenant_id, environment_id, request_id, trace_id,
  virtual_key_id, budget_rule_id, scope_type, scope_id, budget_date,
  spent_before_cny, estimated_request_cost_cny, projected_cost_cny, budget_cny,
  threshold_percent, budget_status, action_taken, reason_code, created_at
)
SELECT
  be.budget_evaluation_id,
  be.tenant_id,
  br.environment_id,
  be.request_id,
  be.trace_id,
  be.virtual_key_id,
  be.budget_rule_id,
  be.scope_type,
  be.scope_id,
  be.budget_date,
  be.spent_before_cny,
  be.estimated_request_cost_cny,
  be.projected_cost_cny,
  be.budget_cny,
  be.threshold_percent,
  be.budget_status,
  be.action_taken,
  be.reason_code,
  be.created_at
FROM budget_evaluations be
JOIN budget_rules_environment_new br ON br.budget_rule_id=be.budget_rule_id;

-- 删除旧预算历史表：新表已经完成数据复制，按外键依赖从子表到主表删除。
DROP TABLE budget_alert_events;

-- 表删除说明：预算命中记录已复制到包含环境的新表。
DROP TABLE budget_evaluations;

-- 表删除说明：旧预算规则唯一约束不再满足多环境需求。
DROP TABLE budget_rules;

-- 重命名预算规则新表：恢复稳定业务表名。
ALTER TABLE budget_rules_environment_new RENAME TO budget_rules;

-- 重命名预算告警事件新表：恢复稳定业务表名。
ALTER TABLE budget_alert_events_environment_new RENAME TO budget_alert_events;

-- 重命名预算命中记录新表：恢复稳定业务表名。
ALTER TABLE budget_evaluations_environment_new RENAME TO budget_evaluations;

-- 创建 Environment 主查询索引：支撑租户内状态和标准代码筛选。
CREATE INDEX IF NOT EXISTS idx_environments_tenant_status
  ON environments(tenant_id, status, code);

-- 创建环境事件索引：支撑按租户和时间查询审计事件。
CREATE INDEX IF NOT EXISTS idx_environment_events_tenant_time
  ON environment_events(tenant_id, created_at DESC);

-- 创建 Key 环境索引：环境设置页统计和数据面管理查询均使用 tenant_id + environment_id。
CREATE INDEX IF NOT EXISTS idx_virtual_keys_environment_id
  ON virtual_keys(tenant_id, environment_id, status);

-- 创建模型别名环境唯一索引：Environment ID 成为权威隔离维度。
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_aliases_environment_alias
  ON model_aliases(tenant_id, environment_id, alias);

-- 创建路由决策环境索引：支撑按环境排查路由失败。
CREATE INDEX IF NOT EXISTS idx_routing_decisions_environment_time
  ON routing_decisions(tenant_id, environment_id, created_at DESC);

-- 创建调用日志环境索引：支撑控制台按环境和时间分页。
CREATE INDEX IF NOT EXISTS idx_request_logs_environment_time
  ON request_logs(tenant_id, environment_id, created_at DESC);

-- 创建用量环境索引：支撑环境成本聚合和预算消费计算。
CREATE INDEX IF NOT EXISTS idx_usage_ledger_environment_time
  ON usage_ledger(tenant_id, environment_id, created_at DESC);

-- 重建预算规则查询索引：环境必须进入数据面预算加载条件。
CREATE INDEX IF NOT EXISTS idx_budget_rules_tenant_environment_scope
  ON budget_rules(tenant_id, environment_id, scope_type, scope_id);

-- 重建预算状态索引：控制台按环境和状态筛选预算。
CREATE INDEX IF NOT EXISTS idx_budget_rules_environment_status
  ON budget_rules(tenant_id, environment_id, status);

-- 重建预算事件索引：保留原有业务日和 request_id 查询能力。
CREATE INDEX IF NOT EXISTS idx_budget_alert_events_tenant_date
  ON budget_alert_events(tenant_id, budget_date DESC);

-- 索引创建说明：支持从失败调用跳转到首次触发的预算事件。
CREATE INDEX IF NOT EXISTS idx_budget_alert_events_request_id
  ON budget_alert_events(request_id);

-- 重建预算命中索引：调用日志详情按 request_id 读取预算解释。
CREATE INDEX IF NOT EXISTS idx_budget_evaluations_request_id
  ON budget_evaluations(tenant_id, request_id);

-- 索引创建说明：支持预算页面按环境和业务日检索命中记录。
CREATE INDEX IF NOT EXISTS idx_budget_evaluations_environment_date
  ON budget_evaluations(tenant_id, environment_id, budget_date DESC);

-- 提交全部环境隔离结构和数据回填，保证迁移原子性。
COMMIT;

-- 迁移完成后恢复外键检查，后续业务连接继续执行租户与资源完整性约束。
PRAGMA foreign_keys=on;
