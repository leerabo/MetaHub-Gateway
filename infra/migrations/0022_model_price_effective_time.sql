-- 迁移说明：PRD-031 新增有效期价格计划，并让每请求价格快照记录实际采用的计划。

PRAGMA foreign_keys=off;
BEGIN IMMEDIATE;

-- 价格计划主表：保存租户内模型的历史、当前和未来人工价格，不覆盖已入账快照。
CREATE TABLE IF NOT EXISTS model_price_plans (
  price_plan_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  input_price_per_1k NUMERIC NOT NULL CHECK (input_price_per_1k >= 0),
  output_price_per_1k NUMERIC NOT NULL CHECK (output_price_per_1k >= 0),
  currency TEXT NOT NULL CHECK (currency IN ('CNY', 'USD')),
  price_schema_version TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual')),
  created_by TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (model_id) REFERENCES models(model_id),
  FOREIGN KEY (created_by) REFERENCES users(user_id),
  UNIQUE (tenant_id, model_id, effective_from),
  UNIQUE (tenant_id, model_id, price_schema_version)
);

-- 价格事件表：记录价格计划创建链路，事件正文只允许保存安全财务元数据。
CREATE TABLE IF NOT EXISTS model_price_events (
  price_event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  price_plan_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('price.updated')),
  actor_id TEXT,
  request_id TEXT,
  trace_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (price_plan_id) REFERENCES model_price_plans(price_plan_id),
  FOREIGN KEY (model_id) REFERENCES models(model_id),
  FOREIGN KEY (actor_id) REFERENCES users(user_id)
);

-- 快照关联说明：记录本次成本采用的有效期计划，旧快照保持空值并继续按原字段解释。
ALTER TABLE price_snapshots ADD COLUMN price_plan_id TEXT REFERENCES model_price_plans(price_plan_id);

-- 快照时间说明：冗余计划生效时刻，便于账本详情在计划表不可用时仍可解释。
ALTER TABLE price_snapshots ADD COLUMN effective_from TEXT;

-- 查询索引：Gateway 按租户、模型和服务器时间选择最近一条已生效价格计划。
CREATE INDEX IF NOT EXISTS idx_model_price_plans_effective
  ON model_price_plans(tenant_id, model_id, effective_from DESC);

-- 查询索引：控制台按租户分页浏览价格变更历史和未来计划。
CREATE INDEX IF NOT EXISTS idx_model_price_plans_tenant_created
  ON model_price_plans(tenant_id, created_at DESC);

-- 审计索引：支持按租户和创建时间追踪 price.updated 事件。
CREATE INDEX IF NOT EXISTS idx_model_price_events_tenant_time
  ON model_price_events(tenant_id, created_at DESC);

-- 快照索引：支持从价格计划反查已采用该计划的不可变 Usage 快照。
CREATE INDEX IF NOT EXISTS idx_price_snapshots_price_plan
  ON price_snapshots(tenant_id, price_plan_id, created_at DESC);

COMMIT;
PRAGMA foreign_keys=on;
