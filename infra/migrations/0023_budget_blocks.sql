-- 迁移说明：PRD-033 增加预算硬阻断生命周期和 blocked/unblocked 事件事实，支持提额或切换模式后恢复。

PRAGMA foreign_keys=off;
BEGIN IMMEDIATE;

-- 阻断生命周期表：一行表示从首次硬阻断到解除的完整过程，所有状态均按租户和 Environment 隔离。
CREATE TABLE IF NOT EXISTS budget_blocks (
  budget_block_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  budget_rule_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'project', 'application', 'virtual_key')),
  scope_id TEXT NOT NULL,
  budget_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'released')),
  blocked_cost_cny NUMERIC NOT NULL DEFAULT 0 CHECK (blocked_cost_cny >= 0),
  budget_cny NUMERIC NOT NULL CHECK (budget_cny > 0),
  blocked_request_id TEXT NOT NULL,
  blocked_trace_id TEXT NOT NULL,
  blocked_at TEXT NOT NULL,
  release_reason TEXT CHECK (
    release_reason IS NULL OR release_reason IN (
      'budget_raised', 'mode_alert_only', 'rule_disabled', 'below_threshold', 'business_day_ended'
    )
  ),
  released_by TEXT,
  release_request_id TEXT,
  release_trace_id TEXT,
  released_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules(budget_rule_id),
  FOREIGN KEY (released_by) REFERENCES users(user_id)
);

-- 阻断事件事实表：blocked/unblocked 各保存一次，后续异步 Webhook 投递可从该表可靠消费。
CREATE TABLE IF NOT EXISTS budget_block_events (
  budget_block_event_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  budget_block_id TEXT NOT NULL,
  budget_rule_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('budget.blocked', 'budget.unblocked')),
  actor_id TEXT,
  request_id TEXT,
  trace_id TEXT,
  reason_code TEXT NOT NULL,
  current_cost_cny NUMERIC NOT NULL DEFAULT 0 CHECK (current_cost_cny >= 0),
  budget_cny NUMERIC NOT NULL CHECK (budget_cny > 0),
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY (budget_block_id) REFERENCES budget_blocks(budget_block_id),
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules(budget_rule_id),
  FOREIGN KEY (actor_id) REFERENCES users(user_id),
  UNIQUE (tenant_id, budget_block_id, event_type)
);

-- 准入预留表：Provider 调用前持久化本次估算成本。后续聚合仅统计尚未出现 Usage 的请求，
-- 因此 Usage 成功入账后由真实成本自然替代预留；账本写失败时预留仍保守占用预算。
CREATE TABLE IF NOT EXISTS budget_reservations (
  budget_reservation_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  budget_rule_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'project', 'application', 'virtual_key')),
  scope_id TEXT NOT NULL,
  budget_date TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  reserved_cost_cny NUMERIC NOT NULL CHECK (reserved_cost_cny >= 0),
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules(budget_rule_id),
  UNIQUE (tenant_id, budget_rule_id, request_id)
);

-- SQLite 单列外键只能证明 ID 存在，不能证明冗余的 tenant/environment 属于同一预算规则。
-- 以下触发器同时覆盖 INSERT 和归属字段 UPDATE，拒绝跨 Environment 或跨租户伪造关联。
CREATE TRIGGER IF NOT EXISTS trg_budget_blocks_owner_insert
BEFORE INSERT ON budget_blocks
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules r
  JOIN environments e ON e.environment_id=r.environment_id AND e.tenant_id=r.tenant_id
  WHERE r.budget_rule_id=NEW.budget_rule_id
    AND r.tenant_id=NEW.tenant_id
    AND r.environment_id=NEW.environment_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_block_owner_mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_budget_blocks_owner_update
BEFORE UPDATE OF tenant_id, environment_id, budget_rule_id ON budget_blocks
WHEN NOT EXISTS (
       SELECT 1 FROM budget_rules r
       JOIN environments e ON e.environment_id=r.environment_id AND e.tenant_id=r.tenant_id
       WHERE r.budget_rule_id=NEW.budget_rule_id
         AND r.tenant_id=NEW.tenant_id
         AND r.environment_id=NEW.environment_id
     )
  OR EXISTS (
       SELECT 1 FROM budget_block_events event
       WHERE event.budget_block_id=OLD.budget_block_id
         AND (
           event.tenant_id<>NEW.tenant_id
           OR event.environment_id<>NEW.environment_id
           OR event.budget_rule_id<>NEW.budget_rule_id
         )
     )
BEGIN
  SELECT RAISE(ABORT, 'budget_block_owner_mismatch');
END;

-- 事件必须与父 BudgetBlock 的 tenant/environment/rule 三元组完全一致，不能只凭 block ID 挂靠。
CREATE TRIGGER IF NOT EXISTS trg_budget_block_events_owner_insert
BEFORE INSERT ON budget_block_events
WHEN NOT EXISTS (
  SELECT 1 FROM budget_blocks b
  WHERE b.budget_block_id=NEW.budget_block_id
    AND b.tenant_id=NEW.tenant_id
    AND b.environment_id=NEW.environment_id
    AND b.budget_rule_id=NEW.budget_rule_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_block_event_owner_mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_budget_block_events_owner_update
BEFORE UPDATE OF tenant_id, environment_id, budget_block_id, budget_rule_id ON budget_block_events
WHEN NOT EXISTS (
  SELECT 1 FROM budget_blocks b
  WHERE b.budget_block_id=NEW.budget_block_id
    AND b.tenant_id=NEW.tenant_id
    AND b.environment_id=NEW.environment_id
    AND b.budget_rule_id=NEW.budget_rule_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_block_event_owner_mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_budget_reservations_owner_insert
BEFORE INSERT ON budget_reservations
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules r
  JOIN environments e ON e.environment_id=r.environment_id AND e.tenant_id=r.tenant_id
  WHERE r.budget_rule_id=NEW.budget_rule_id
    AND r.tenant_id=NEW.tenant_id
    AND r.environment_id=NEW.environment_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_reservation_owner_mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_budget_reservations_owner_update
BEFORE UPDATE OF tenant_id, environment_id, budget_rule_id ON budget_reservations
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules r
  JOIN environments e ON e.environment_id=r.environment_id AND e.tenant_id=r.tenant_id
  WHERE r.budget_rule_id=NEW.budget_rule_id
    AND r.tenant_id=NEW.tenant_id
    AND r.environment_id=NEW.environment_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_reservation_owner_mismatch');
END;

-- 并发幂等索引：同一租户、规则和业务日最多存在一个 active 阻断，释放后允许重新阻断。
CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_blocks_one_active_rule_day
  ON budget_blocks(tenant_id, budget_rule_id, budget_date)
  WHERE status='active';

-- 控制面查询索引：预算规则列表按租户和规则关联当前 active block。
CREATE INDEX IF NOT EXISTS idx_budget_blocks_tenant_rule_status
  ON budget_blocks(tenant_id, budget_rule_id, status, blocked_at DESC);

-- 数据面查询索引：Guard 按 Environment 和业务日快速读取 active 阻断。
CREATE INDEX IF NOT EXISTS idx_budget_blocks_environment_date
  ON budget_blocks(tenant_id, environment_id, budget_date, status);

-- 审计事件索引：支持按租户、事件类型和时间查询阻断/恢复轨迹。
CREATE INDEX IF NOT EXISTS idx_budget_block_events_tenant_time
  ON budget_block_events(tenant_id, event_type, created_at DESC);

-- 准入热路径按规则和业务日扫描未结算预留；request_id 索引用于阻断时清理当前请求。
CREATE INDEX IF NOT EXISTS idx_budget_reservations_rule_date
  ON budget_reservations(tenant_id, budget_rule_id, budget_date);
-- 请求预留索引：支持 Provider 前失败或后序规则阻断时按 request_id 清理本次预留。
CREATE INDEX IF NOT EXISTS idx_budget_reservations_request
  ON budget_reservations(tenant_id, request_id);

COMMIT;
PRAGMA foreign_keys=on;
