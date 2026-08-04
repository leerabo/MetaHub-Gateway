-- 迁移职责：为 PRD-045 增加不可变策略快照、租户级请求认领和请求日志版本追踪。
-- 安全边界：所有关联使用 tenant_id 复合键；快照正文仅保存受限 DSL canonical 表达式，
-- 不保存 Prompt、Response、Provider Key、Virtual Key 明文或任何 Token。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- claim 需要把 Virtual Key 归属编码为复合外键；该候选键必须先于 claim 表创建。
CREATE UNIQUE INDEX uq_virtual_keys_tenant_key_prd045
  ON virtual_keys(tenant_id, virtual_key_id);

-- 快照 header 先以 building 写入，事务内补齐 items 后才允许一次性封印为 published。
CREATE TABLE policy_snapshots (
  policy_snapshot_id TEXT PRIMARY KEY CHECK (
    length(policy_snapshot_id) BETWEEN 7 AND 128 AND policy_snapshot_id GLOB 'psnap_*'
  ),
  tenant_id TEXT NOT NULL,
  snapshot_version INTEGER NOT NULL CHECK (
    typeof(snapshot_version) = 'integer' AND snapshot_version >= 1
  ),
  schema_version TEXT NOT NULL CHECK (schema_version = 'policy-snapshot-v1'),
  state TEXT NOT NULL CHECK (state IN ('building', 'published')),
  operation TEXT NOT NULL CHECK (operation IN ('publish', 'rollback')),
  previous_snapshot_id TEXT,
  previous_snapshot_version INTEGER CHECK (
    previous_snapshot_version IS NULL
    OR (typeof(previous_snapshot_version) = 'integer' AND previous_snapshot_version >= 1)
  ),
  rollback_source_snapshot_id TEXT,
  rollback_source_snapshot_version INTEGER CHECK (
    rollback_source_snapshot_version IS NULL
    OR (typeof(rollback_source_snapshot_version) = 'integer'
        AND rollback_source_snapshot_version >= 1)
  ),
  request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
  request_fingerprint TEXT NOT NULL CHECK (
    length(request_fingerprint) = 64
    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  content_hash TEXT CHECK (
    content_hash IS NULL
    OR (length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*')
  ),
  artifact_hash TEXT CHECK (
    artifact_hash IS NULL
    OR (length(artifact_hash) = 64 AND artifact_hash NOT GLOB '*[^0-9a-f]*')
  ),
  policy_count INTEGER CHECK (
    policy_count IS NULL OR (typeof(policy_count) = 'integer' AND policy_count >= 0)
  ),
  change_reason TEXT NOT NULL CHECK (
    length(change_reason) BETWEEN 1 AND 256
    AND instr(change_reason, char(0)) = 0
    AND instr(change_reason, char(9)) = 0
    AND instr(change_reason, char(10)) = 0
    AND instr(change_reason, char(13)) = 0
  ),
  approval_status TEXT NOT NULL CHECK (approval_status = 'not_required'),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  published_at TEXT CHECK (published_at IS NULL OR length(published_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, created_by) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, previous_snapshot_id, previous_snapshot_version)
    REFERENCES policy_snapshots(tenant_id, policy_snapshot_id, snapshot_version),
  FOREIGN KEY (
    tenant_id, rollback_source_snapshot_id, rollback_source_snapshot_version
  ) REFERENCES policy_snapshots(tenant_id, policy_snapshot_id, snapshot_version),
  UNIQUE (tenant_id, policy_snapshot_id),
  UNIQUE (tenant_id, snapshot_version),
  UNIQUE (tenant_id, policy_snapshot_id, snapshot_version),
  UNIQUE (tenant_id, request_id),
  CHECK (
    (previous_snapshot_id IS NULL AND previous_snapshot_version IS NULL)
    OR (previous_snapshot_id IS NOT NULL AND previous_snapshot_version IS NOT NULL)
  ),
  CHECK (
    (snapshot_version = 1
      AND previous_snapshot_id IS NULL AND previous_snapshot_version IS NULL)
    OR (snapshot_version > 1
      AND previous_snapshot_id IS NOT NULL
      AND previous_snapshot_version = snapshot_version - 1)
  ),
  CHECK (
    (operation = 'publish'
      AND rollback_source_snapshot_id IS NULL
      AND rollback_source_snapshot_version IS NULL)
    OR (operation = 'rollback'
      AND rollback_source_snapshot_id IS NOT NULL
      AND rollback_source_snapshot_version IS NOT NULL
      AND rollback_source_snapshot_version < snapshot_version)
  ),
  CHECK (
    (state = 'building'
      AND content_hash IS NULL AND artifact_hash IS NULL
      AND policy_count IS NULL AND published_at IS NULL)
    OR (state = 'published'
      AND content_hash IS NOT NULL AND artifact_hash IS NOT NULL
      AND policy_count IS NOT NULL AND published_at IS NOT NULL)
  )
);

CREATE INDEX idx_policy_snapshots_tenant_version
  ON policy_snapshots(tenant_id, snapshot_version DESC);
CREATE INDEX idx_policy_snapshots_tenant_created
  ON policy_snapshots(tenant_id, created_at DESC, policy_snapshot_id DESC);

-- 插入只能预留当前 head 的下一连续版本；BEGIN IMMEDIATE 负责串行化并发发布事务。
CREATE TRIGGER trg_policy_snapshots_sequence_insert
BEFORE INSERT ON policy_snapshots
FOR EACH ROW
WHEN
  NEW.state != 'building'
  OR (
    NEW.snapshot_version = 1
    AND EXISTS (
      SELECT 1 FROM policy_snapshot_heads h WHERE h.tenant_id = NEW.tenant_id
    )
  )
  OR (
    NEW.snapshot_version > 1
    AND NOT EXISTS (
      SELECT 1 FROM policy_snapshot_heads h
      WHERE h.tenant_id = NEW.tenant_id
        AND h.current_snapshot_id = NEW.previous_snapshot_id
        AND h.current_snapshot_version = NEW.previous_snapshot_version
    )
  )
  OR (
    NEW.operation = 'rollback'
    AND NOT EXISTS (
      SELECT 1
      FROM policy_snapshot_heads h
      JOIN policy_snapshots current_snapshot
        ON current_snapshot.tenant_id = h.tenant_id
       AND current_snapshot.policy_snapshot_id = h.current_snapshot_id
       AND current_snapshot.snapshot_version = h.current_snapshot_version
      WHERE h.tenant_id = NEW.tenant_id
        AND current_snapshot.previous_snapshot_id = NEW.rollback_source_snapshot_id
        AND current_snapshot.previous_snapshot_version = NEW.rollback_source_snapshot_version
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_sequence_invalid');
END;

-- 快照项复制 Policy 的执行事实；原始 condition_expr、描述和任意请求正文均不进入快照。
CREATE TABLE policy_snapshot_items (
  policy_snapshot_item_id TEXT PRIMARY KEY CHECK (
    length(policy_snapshot_item_id) BETWEEN 8 AND 128
    AND policy_snapshot_item_id GLOB 'psitem_*'
  ),
  tenant_id TEXT NOT NULL,
  policy_snapshot_id TEXT NOT NULL,
  evaluation_order INTEGER NOT NULL CHECK (
    typeof(evaluation_order) = 'integer' AND evaluation_order >= 0
  ),
  source_policy_id TEXT NOT NULL CHECK (length(source_policy_id) BETWEEN 5 AND 128),
  source_policy_version INTEGER NOT NULL CHECK (
    typeof(source_policy_version) = 'integer' AND source_policy_version >= 1
  ),
  policy_rule_id TEXT NOT NULL CHECK (length(policy_rule_id) BETWEEN 7 AND 128),
  policy_action_id TEXT NOT NULL CHECK (length(policy_action_id) BETWEEN 6 AND 128),
  priority INTEGER NOT NULL CHECK (
    typeof(priority) = 'integer' AND priority BETWEEN 1 AND 10000
  ),
  dsl_version TEXT NOT NULL CHECK (dsl_version = 'policy-dsl-v1'),
  canonical_expression TEXT NOT NULL CHECK (
    length(canonical_expression) BETWEEN 1 AND 1024
    AND length(CAST(canonical_expression AS BLOB)) <= 2048
    AND instr(canonical_expression, char(0)) = 0
  ),
  condition_hash TEXT NOT NULL CHECK (
    length(condition_hash) = 64 AND condition_hash NOT GLOB '*[^0-9a-f]*'
  ),
  variables_json TEXT NOT NULL CHECK (
    json_valid(variables_json)
    AND json_type(variables_json) = 'array'
    AND json_array_length(variables_json) <= 6
    AND length(CAST(variables_json AS BLOB)) <= 4096
  ),
  action_type TEXT NOT NULL CHECK (
    action_type IN ('allow', 'deny', 'mask', 'log_only', 'route_to')
  ),
  target_model_id TEXT,
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, policy_snapshot_id)
    REFERENCES policy_snapshots(tenant_id, policy_snapshot_id),
  FOREIGN KEY (tenant_id, source_policy_id) REFERENCES policies(tenant_id, policy_id),
  FOREIGN KEY (tenant_id, target_model_id) REFERENCES models(tenant_id, model_id),
  UNIQUE (tenant_id, policy_snapshot_id, evaluation_order),
  UNIQUE (tenant_id, policy_snapshot_id, source_policy_id),
  CHECK (
    (action_type = 'route_to' AND target_model_id IS NOT NULL)
    OR (action_type != 'route_to' AND target_model_id IS NULL)
  )
);

CREATE INDEX idx_policy_snapshot_items_tenant_snapshot
  ON policy_snapshot_items(tenant_id, policy_snapshot_id, evaluation_order);

-- publish 项必须与当前 active Policy 图一致；服务层重编译之外，DB 再阻断伪造来源。
CREATE TRIGGER trg_policy_snapshot_items_publish_insert
BEFORE INSERT ON policy_snapshot_items
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_snapshots snapshot
  WHERE snapshot.tenant_id = NEW.tenant_id
    AND snapshot.policy_snapshot_id = NEW.policy_snapshot_id
    AND snapshot.operation = 'publish'
)
AND NOT EXISTS (
  SELECT 1
  FROM policies p
  JOIN policy_rules r ON r.tenant_id = p.tenant_id AND r.policy_id = p.policy_id
  JOIN policy_actions a ON a.tenant_id = p.tenant_id AND a.policy_id = p.policy_id
  WHERE p.tenant_id = NEW.tenant_id
    AND p.policy_id = NEW.source_policy_id
    AND p.status = 'active'
    AND p.version = NEW.source_policy_version
    AND p.priority = NEW.priority
    AND p.dsl_version = NEW.dsl_version
    AND r.policy_rule_id = NEW.policy_rule_id
    AND r.condition_hash = NEW.condition_hash
    AND json(r.variables_json) = json(NEW.variables_json)
    AND a.policy_action_id = NEW.policy_action_id
    AND a.action_type = NEW.action_type
    AND a.target_model_id IS NEW.target_model_id
)
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_item_source_mismatch');
END;

-- rollback 项只能逐字段复制指定来源快照；不能把任意新内容伪装成历史回滚。
CREATE TRIGGER trg_policy_snapshot_items_rollback_insert
BEFORE INSERT ON policy_snapshot_items
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_snapshots snapshot
  WHERE snapshot.tenant_id = NEW.tenant_id
    AND snapshot.policy_snapshot_id = NEW.policy_snapshot_id
    AND snapshot.operation = 'rollback'
)
AND NOT EXISTS (
  SELECT 1
  FROM policy_snapshots snapshot
  JOIN policy_snapshot_items source
    ON source.tenant_id = snapshot.tenant_id
   AND source.policy_snapshot_id = snapshot.rollback_source_snapshot_id
  WHERE snapshot.tenant_id = NEW.tenant_id
    AND snapshot.policy_snapshot_id = NEW.policy_snapshot_id
    AND source.evaluation_order = NEW.evaluation_order
    AND source.source_policy_id = NEW.source_policy_id
    AND source.source_policy_version = NEW.source_policy_version
    AND source.policy_rule_id = NEW.policy_rule_id
    AND source.policy_action_id = NEW.policy_action_id
    AND source.priority = NEW.priority
    AND source.dsl_version = NEW.dsl_version
    AND source.canonical_expression = NEW.canonical_expression
    AND source.condition_hash = NEW.condition_hash
    AND json(source.variables_json) = json(NEW.variables_json)
    AND source.action_type = NEW.action_type
    AND source.target_model_id IS NEW.target_model_id
)
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_rollback_item_mismatch');
END;

-- 无论 publish 或 rollback，item 只能写入 building，route_to 目标必须此刻仍属于租户且 active。
CREATE TRIGGER trg_policy_snapshot_items_guard_insert
BEFORE INSERT ON policy_snapshot_items
FOR EACH ROW
WHEN
  NOT EXISTS (
    SELECT 1 FROM policy_snapshots snapshot
    WHERE snapshot.tenant_id = NEW.tenant_id
      AND snapshot.policy_snapshot_id = NEW.policy_snapshot_id
      AND snapshot.state = 'building'
  )
  OR (
    NEW.action_type = 'route_to'
    AND NOT EXISTS (
      SELECT 1 FROM models model
      WHERE model.tenant_id = NEW.tenant_id
        AND model.model_id = NEW.target_model_id
        AND model.status = 'active'
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_item_owner_mismatch');
END;

CREATE TRIGGER trg_policy_snapshot_items_immutable_update
BEFORE UPDATE ON policy_snapshot_items
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_item_immutable');
END;

-- 仅异常 building 可按“先 item 后 header”受控清理；published 制品永远不可删除。
CREATE TRIGGER trg_policy_snapshot_items_delete_guard
BEFORE DELETE ON policy_snapshot_items
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM policy_snapshots snapshot
  WHERE snapshot.tenant_id = OLD.tenant_id
    AND snapshot.policy_snapshot_id = OLD.policy_snapshot_id
    AND snapshot.state = 'building'
)
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_item_immutable');
END;

-- seal 只能从 building 精确转为 published；其他列 NULL-safe 不变，且 items 顺序必须连续。
CREATE TRIGGER trg_policy_snapshots_seal_update
BEFORE UPDATE ON policy_snapshots
FOR EACH ROW
WHEN NOT (
  OLD.state = 'building'
  AND NEW.state = 'published'
  AND NEW.policy_snapshot_id IS OLD.policy_snapshot_id
  AND NEW.tenant_id IS OLD.tenant_id
  AND NEW.snapshot_version IS OLD.snapshot_version
  AND NEW.schema_version IS OLD.schema_version
  AND NEW.operation IS OLD.operation
  AND NEW.previous_snapshot_id IS OLD.previous_snapshot_id
  AND NEW.previous_snapshot_version IS OLD.previous_snapshot_version
  AND NEW.rollback_source_snapshot_id IS OLD.rollback_source_snapshot_id
  AND NEW.rollback_source_snapshot_version IS OLD.rollback_source_snapshot_version
  AND NEW.request_id IS OLD.request_id
  AND NEW.trace_id IS OLD.trace_id
  AND NEW.request_fingerprint IS OLD.request_fingerprint
  AND NEW.change_reason IS OLD.change_reason
  AND NEW.approval_status IS OLD.approval_status
  AND NEW.created_by IS OLD.created_by
  AND NEW.created_at IS OLD.created_at
  AND NEW.policy_count = (
    SELECT COUNT(*) FROM policy_snapshot_items item
    WHERE item.tenant_id = OLD.tenant_id
      AND item.policy_snapshot_id = OLD.policy_snapshot_id
  )
  AND (
    NEW.policy_count = 0
    OR (
      (SELECT MIN(item.evaluation_order) FROM policy_snapshot_items item
       WHERE item.tenant_id = OLD.tenant_id
         AND item.policy_snapshot_id = OLD.policy_snapshot_id) = 0
      AND (SELECT MAX(item.evaluation_order) FROM policy_snapshot_items item
           WHERE item.tenant_id = OLD.tenant_id
             AND item.policy_snapshot_id = OLD.policy_snapshot_id) = NEW.policy_count - 1
      AND (SELECT COUNT(DISTINCT item.evaluation_order) FROM policy_snapshot_items item
           WHERE item.tenant_id = OLD.tenant_id
             AND item.policy_snapshot_id = OLD.policy_snapshot_id) = NEW.policy_count
    )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_immutable');
END;

CREATE TRIGGER trg_policy_snapshots_delete_guard
BEFORE DELETE ON policy_snapshots
FOR EACH ROW
WHEN
  OLD.state = 'published'
  OR EXISTS (
    SELECT 1 FROM policy_snapshot_heads h
    WHERE h.tenant_id = OLD.tenant_id AND h.current_snapshot_id = OLD.policy_snapshot_id
  )
  OR EXISTS (
    SELECT 1 FROM policy_snapshot_events event
    WHERE event.tenant_id = OLD.tenant_id
      AND event.policy_snapshot_id = OLD.policy_snapshot_id
  )
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_immutable');
END;

-- 每租户 head 是数据面 O(1) 拉取入口，只能指向已封印的当前连续版本。
CREATE TABLE policy_snapshot_heads (
  tenant_id TEXT PRIMARY KEY,
  current_snapshot_id TEXT NOT NULL,
  current_snapshot_version INTEGER NOT NULL CHECK (
    typeof(current_snapshot_version) = 'integer' AND current_snapshot_version >= 1
  ),
  updated_by TEXT NOT NULL,
  updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, current_snapshot_id, current_snapshot_version)
    REFERENCES policy_snapshots(tenant_id, policy_snapshot_id, snapshot_version),
  FOREIGN KEY (tenant_id, updated_by) REFERENCES users(tenant_id, user_id)
);

CREATE TRIGGER trg_policy_snapshot_heads_insert
BEFORE INSERT ON policy_snapshot_heads
FOR EACH ROW
WHEN
  NEW.current_snapshot_version != 1
  OR NOT EXISTS (
    SELECT 1 FROM policy_snapshots snapshot
    WHERE snapshot.tenant_id = NEW.tenant_id
      AND snapshot.policy_snapshot_id = NEW.current_snapshot_id
      AND snapshot.snapshot_version = NEW.current_snapshot_version
      AND snapshot.state = 'published'
  )
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_head_invalid');
END;

CREATE TRIGGER trg_policy_snapshot_heads_update
BEFORE UPDATE ON policy_snapshot_heads
FOR EACH ROW
WHEN
  NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.current_snapshot_version != OLD.current_snapshot_version + 1
  OR NOT EXISTS (
    SELECT 1 FROM policy_snapshots snapshot
    WHERE snapshot.tenant_id = NEW.tenant_id
      AND snapshot.policy_snapshot_id = NEW.current_snapshot_id
      AND snapshot.snapshot_version = NEW.current_snapshot_version
      AND snapshot.previous_snapshot_id = OLD.current_snapshot_id
      AND snapshot.previous_snapshot_version = OLD.current_snapshot_version
      AND snapshot.state = 'published'
  )
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_head_invalid');
END;

CREATE TRIGGER trg_policy_snapshot_heads_delete
BEFORE DELETE ON policy_snapshot_heads
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_head_immutable');
END;

-- 事件是发布事务的持久追加事实；payload 仅允许保存版本、hash、数量和回滚来源摘要。
CREATE TABLE policy_snapshot_events (
  policy_snapshot_event_id TEXT PRIMARY KEY CHECK (
    length(policy_snapshot_event_id) BETWEEN 7 AND 128
    AND policy_snapshot_event_id GLOB 'psevt_*'
  ),
  tenant_id TEXT NOT NULL,
  policy_snapshot_id TEXT NOT NULL,
  snapshot_version INTEGER NOT NULL CHECK (
    typeof(snapshot_version) = 'integer' AND snapshot_version >= 1
  ),
  event_type TEXT NOT NULL CHECK (event_type = 'policy_snapshot.published'),
  operation TEXT NOT NULL CHECK (operation IN ('publish', 'rollback')),
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
  payload_json TEXT NOT NULL CHECK (
    json_valid(payload_json)
    AND json_type(payload_json) = 'object'
    AND length(CAST(payload_json AS BLOB)) <= 8192
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, policy_snapshot_id, snapshot_version)
    REFERENCES policy_snapshots(tenant_id, policy_snapshot_id, snapshot_version),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  UNIQUE (tenant_id, policy_snapshot_id, event_type),
  UNIQUE (tenant_id, request_id, event_type)
);

CREATE INDEX idx_policy_snapshot_events_tenant_time
  ON policy_snapshot_events(tenant_id, created_at DESC, policy_snapshot_event_id DESC);

CREATE TRIGGER trg_policy_snapshot_events_insert
BEFORE INSERT ON policy_snapshot_events
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM policy_snapshots snapshot
  WHERE snapshot.tenant_id = NEW.tenant_id
    AND snapshot.policy_snapshot_id = NEW.policy_snapshot_id
    AND snapshot.snapshot_version = NEW.snapshot_version
    AND snapshot.state = 'published'
    AND snapshot.operation = NEW.operation
)
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_event_owner_mismatch');
END;

CREATE TRIGGER trg_policy_snapshot_events_immutable_update
BEFORE UPDATE ON policy_snapshot_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_event_immutable');
END;

CREATE TRIGGER trg_policy_snapshot_events_immutable_delete
BEFORE DELETE ON policy_snapshot_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_event_immutable');
END;

-- 先复制 Usage，再替换 request_logs；外键关闭期间不存在应用写入，提交后恢复为复合租户请求键。
-- 外部 sequence 触发器的 SQL 引用 usage_ledger；SQLite rename 会在缺表窗口校验它，必须先解除。
DROP TRIGGER IF EXISTS trg_usage_export_ledger_sequences_owner_insert;
DROP TRIGGER IF EXISTS trg_usage_export_ledger_sequences_immutable_update;
DROP TRIGGER IF EXISTS trg_usage_export_ledger_sequences_immutable_delete;

-- price_snapshots 也引用 request_logs.request_id；必须同步升级为 tenant+request 复合外键。
DROP TABLE IF EXISTS price_snapshots_prd045_new;
CREATE TABLE price_snapshots_prd045_new (
  price_snapshot_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT,
  model_id TEXT,
  provider_id TEXT,
  provider_code TEXT,
  currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency IN ('CNY', 'USD')),
  input_price_per_1k NUMERIC NOT NULL DEFAULT 0 CHECK (input_price_per_1k >= 0),
  output_price_per_1k NUMERIC NOT NULL DEFAULT 0 CHECK (output_price_per_1k >= 0),
  price_schema_version TEXT,
  billing_dimensions TEXT NOT NULL DEFAULT '{}',
  provider_raw_mapping TEXT NOT NULL DEFAULT '{}',
  source TEXT NOT NULL DEFAULT 'model_catalog',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  price_plan_id TEXT REFERENCES model_price_plans(price_plan_id),
  effective_from TEXT,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, request_id) REFERENCES request_logs(tenant_id, request_id),
  FOREIGN KEY (model_id) REFERENCES models(model_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id)
);
-- 价格快照逐列复制，模型价格、provider 原始映射和时间事实均不得因请求键升级漂移。
INSERT INTO price_snapshots_prd045_new(
  price_snapshot_id, tenant_id, request_id, model_id, provider_id, provider_code,
  currency, input_price_per_1k, output_price_per_1k, price_schema_version,
  billing_dimensions, provider_raw_mapping, source, status, created_at, updated_at,
  price_plan_id, effective_from
)
SELECT
  price_snapshot_id, tenant_id, request_id, model_id, provider_id, provider_code,
  currency, input_price_per_1k, output_price_per_1k, price_schema_version,
  billing_dimensions, provider_raw_mapping, source, status, created_at, updated_at,
  price_plan_id, effective_from
FROM price_snapshots;
DROP TABLE price_snapshots;

-- 清理异常执行遗留的临时 Usage 表，确保本次事务从唯一确定的重建源开始。
DROP TABLE IF EXISTS usage_ledger_prd045_new;
CREATE TABLE usage_ledger_prd045_new (
  usage_ledger_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  raw_usage TEXT NOT NULL DEFAULT '{}',
  normalized_usage TEXT NOT NULL DEFAULT '{}',
  cost_amount NUMERIC NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'CNY',
  price_schema_version TEXT,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'recorded' CHECK (status IN ('recorded')),
  total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
  cache_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_tokens >= 0),
  cost_cny NUMERIC NOT NULL DEFAULT 0 CHECK (cost_cny >= 0),
  cost_usd NUMERIC NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  estimated INTEGER NOT NULL DEFAULT 0 CHECK (estimated IN (0, 1)),
  usage_source TEXT NOT NULL DEFAULT 'upstream'
    CHECK (usage_source IN ('upstream', 'estimated')),
  price_snapshot_id TEXT,
  model_id TEXT,
  provider_id TEXT,
  virtual_key_id TEXT,
  endpoint TEXT,
  request_model TEXT,
  resolved_model TEXT,
  provider_code TEXT,
  updated_at TEXT,
  project_id TEXT,
  application_id TEXT,
  environment_id TEXT REFERENCES environments(environment_id),
  environment_code TEXT,
  routing_attempt_id TEXT,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, request_id) REFERENCES request_logs(tenant_id, request_id)
);

-- Usage 全部 32 列逐字段复制，不能重新估算 Token、成本或价格来源。
INSERT INTO usage_ledger_prd045_new(
  usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens,
  raw_usage, normalized_usage, cost_amount, currency, price_schema_version,
  created_at, status, total_tokens, cache_tokens, cost_cny, cost_usd,
  estimated, usage_source, price_snapshot_id, model_id, provider_id,
  virtual_key_id, endpoint, request_model, resolved_model, provider_code,
  updated_at, project_id, application_id, environment_id, environment_code,
  routing_attempt_id
)
SELECT
  usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens,
  raw_usage, normalized_usage, cost_amount, currency, price_schema_version,
  created_at, status, total_tokens, cache_tokens, cost_cny, cost_usd,
  estimated, usage_source, price_snapshot_id, model_id, provider_id,
  virtual_key_id, endpoint, request_model, resolved_model, provider_code,
  updated_at, project_id, application_id, environment_id, environment_code,
  routing_attempt_id
FROM usage_ledger;
DROP TABLE usage_ledger;

-- request_logs 由全局 request_id 主键升级为 tenant+request 复合主键，并追加安全 Policy 摘要。
DROP TABLE IF EXISTS request_logs_prd045_new;
CREATE TABLE request_logs_prd045_new (
  request_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  virtual_key_id TEXT,
  provider_id TEXT,
  model_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
  error_code TEXT,
  latency_ms INTEGER,
  created_at TEXT NOT NULL,
  endpoint TEXT,
  request_model TEXT,
  resolved_model TEXT,
  provider_code TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
  estimated_cost_amount NUMERIC NOT NULL DEFAULT 0 CHECK (estimated_cost_amount >= 0),
  currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency IN ('CNY', 'USD')),
  price_schema_version TEXT,
  updated_at TEXT,
  budget_status TEXT,
  budget_rule_id TEXT,
  budget_scope_type TEXT,
  budget_scope_id TEXT,
  budget_action TEXT,
  budget_reason_code TEXT,
  budget_current_cost_cny NUMERIC,
  budget_limit_cny NUMERIC,
  dlp_status TEXT,
  dlp_rule_id TEXT,
  dlp_action TEXT,
  dlp_match_count INTEGER NOT NULL DEFAULT 0,
  dlp_reason_code TEXT,
  project_id TEXT,
  application_id TEXT,
  environment_id TEXT REFERENCES environments(environment_id),
  environment_code TEXT,
  fallback_triggered INTEGER NOT NULL DEFAULT 0 CHECK (fallback_triggered IN (0, 1)),
  routing_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (routing_attempt_count BETWEEN 0 AND 2),
  final_routing_attempt_id TEXT,
  stream_session_id TEXT REFERENCES stream_sessions(stream_session_id),
  stream_status TEXT CHECK (
    stream_status IS NULL
    OR stream_status IN ('completed', 'timed_out', 'client_disconnected', 'upstream_failed')
  ),
  snapshot_version INTEGER CHECK (
    snapshot_version IS NULL
    OR (typeof(snapshot_version) = 'integer' AND snapshot_version >= 1)
  ),
  policy_snapshot_status TEXT NOT NULL DEFAULT 'disabled' CHECK (
    policy_snapshot_status IN (
      'disabled', 'not_published', 'current', 'lkg',
      'emergency_lkg', 'expired', 'unavailable'
    )
  ),
  policy_decision TEXT CHECK (
    policy_decision IS NULL OR policy_decision IN ('allow', 'deny', 'route_to')
  ),
  policy_reason_code TEXT CHECK (
    policy_reason_code IS NULL OR length(policy_reason_code) BETWEEN 1 AND 128
  ),
  policy_id TEXT CHECK (policy_id IS NULL OR length(policy_id) BETWEEN 5 AND 128),
  policy_action_type TEXT CHECK (
    policy_action_type IS NULL
    OR policy_action_type IN ('allow', 'deny', 'mask', 'log_only', 'route_to')
  ),
  policy_target_model_id TEXT CHECK (
    policy_target_model_id IS NULL OR length(policy_target_model_id) BETWEEN 1 AND 128
  ),
  PRIMARY KEY (tenant_id, request_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(virtual_key_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id),
  FOREIGN KEY (model_id) REFERENCES models(model_id),
  CHECK (
    (policy_action_type = 'route_to' AND policy_target_model_id IS NOT NULL)
    OR (policy_action_type IS NULL)
    OR (policy_action_type != 'route_to' AND policy_target_model_id IS NULL)
  )
);

-- 0033 历史统一以 disabled 策略状态迁移；不能伪造曾经执行过任何快照。
INSERT INTO request_logs_prd045_new(
  request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
  status, error_code, latency_ms, created_at, endpoint, request_model,
  resolved_model, provider_code, input_tokens, output_tokens, total_tokens,
  estimated_cost_amount, currency, price_schema_version, updated_at,
  budget_status, budget_rule_id, budget_scope_type, budget_scope_id,
  budget_action, budget_reason_code, budget_current_cost_cny, budget_limit_cny,
  dlp_status, dlp_rule_id, dlp_action, dlp_match_count, dlp_reason_code,
  project_id, application_id, environment_id, environment_code,
  fallback_triggered, routing_attempt_count, final_routing_attempt_id,
  stream_session_id, stream_status, snapshot_version, policy_snapshot_status,
  policy_decision, policy_reason_code, policy_id, policy_action_type,
  policy_target_model_id
)
SELECT
  request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
  status, error_code, latency_ms, created_at, endpoint, request_model,
  resolved_model, provider_code, input_tokens, output_tokens, total_tokens,
  estimated_cost_amount, currency, price_schema_version, updated_at,
  budget_status, budget_rule_id, budget_scope_type, budget_scope_id,
  budget_action, budget_reason_code, budget_current_cost_cny, budget_limit_cny,
  dlp_status, dlp_rule_id, dlp_action, dlp_match_count, dlp_reason_code,
  project_id, application_id, environment_id, environment_code,
  fallback_triggered, routing_attempt_count, final_routing_attempt_id,
  stream_session_id, stream_status, NULL, 'disabled', NULL, NULL, NULL, NULL, NULL
FROM request_logs;
DROP TABLE request_logs;
-- 新表通过复制后才替换稳定名称，应用不会观察到半完成 schema。
ALTER TABLE request_logs_prd045_new RENAME TO request_logs;

-- 恢复 0033 的十五个显式索引，并增加按租户/快照版本查询的 partial index。
CREATE INDEX idx_request_logs_tenant_id ON request_logs(tenant_id);
CREATE INDEX idx_request_logs_trace_id ON request_logs(trace_id);
CREATE INDEX idx_request_logs_error_code ON request_logs(error_code);
CREATE INDEX idx_request_logs_created_at ON request_logs(tenant_id, created_at DESC);
CREATE INDEX idx_request_logs_status_time
  ON request_logs(tenant_id, status, created_at DESC);
CREATE INDEX idx_request_logs_request_model ON request_logs(tenant_id, request_model);
CREATE INDEX idx_request_logs_resolved_model ON request_logs(tenant_id, resolved_model);
CREATE INDEX idx_request_logs_provider_code ON request_logs(tenant_id, provider_code);
CREATE INDEX idx_request_logs_dashboard_latency
  ON request_logs(tenant_id, created_at, latency_ms);
CREATE INDEX idx_request_logs_dashboard_model_time
  ON request_logs(tenant_id, resolved_model, created_at DESC);
CREATE INDEX idx_request_logs_dashboard_key_time
  ON request_logs(tenant_id, virtual_key_id, created_at DESC);
CREATE INDEX idx_request_logs_project_time
  ON request_logs(tenant_id, project_id, created_at DESC);
CREATE INDEX idx_request_logs_application_time
  ON request_logs(tenant_id, application_id, created_at DESC);
CREATE INDEX idx_request_logs_environment_time
  ON request_logs(tenant_id, environment_id, created_at DESC);
CREATE INDEX idx_request_logs_stream_session
  ON request_logs(tenant_id, stream_session_id)
  WHERE stream_session_id IS NOT NULL;
CREATE INDEX idx_request_logs_snapshot_version
  ON request_logs(tenant_id, snapshot_version, created_at DESC, request_id DESC)
  WHERE snapshot_version IS NOT NULL;

-- 恢复最终路由尝试和流会话的四条既有一致性触发器。
CREATE TRIGGER trg_request_log_final_attempt_insert
BEFORE INSERT ON request_logs
FOR EACH ROW
WHEN NEW.final_routing_attempt_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM routing_attempts ra
    WHERE ra.routing_attempt_id=NEW.final_routing_attempt_id
      AND ra.tenant_id=NEW.tenant_id
      AND ra.request_id=NEW.request_id
  ) THEN RAISE(ABORT, 'request_log_final_attempt_mismatch') END;
END;

CREATE TRIGGER trg_request_log_final_attempt_update
BEFORE UPDATE OF final_routing_attempt_id, tenant_id, request_id ON request_logs
FOR EACH ROW
WHEN NEW.final_routing_attempt_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM routing_attempts ra
    WHERE ra.routing_attempt_id=NEW.final_routing_attempt_id
      AND ra.tenant_id=NEW.tenant_id
      AND ra.request_id=NEW.request_id
  ) THEN RAISE(ABORT, 'request_log_final_attempt_mismatch') END;
END;

CREATE TRIGGER trg_request_log_stream_insert
BEFORE INSERT ON request_logs
FOR EACH ROW
BEGIN
  SELECT CASE WHEN (NEW.stream_session_id IS NULL)<>(NEW.stream_status IS NULL)
    THEN RAISE(ABORT, 'request_log_stream_fields_incomplete') END;
  SELECT CASE WHEN NEW.stream_session_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM stream_sessions ss
    WHERE ss.stream_session_id=NEW.stream_session_id
      AND ss.tenant_id=NEW.tenant_id
      AND ss.request_id=NEW.request_id
      AND ss.status=NEW.stream_status
  ) THEN RAISE(ABORT, 'request_log_stream_session_mismatch') END;
  SELECT CASE WHEN NEW.stream_status IS NOT NULL AND NOT (
    (NEW.stream_status='completed' AND NEW.status='success')
    OR (NEW.stream_status<>'completed' AND NEW.status='failed')
  ) THEN RAISE(ABORT, 'request_log_stream_status_mismatch') END;
END;

CREATE TRIGGER trg_request_log_stream_update
BEFORE UPDATE OF tenant_id, request_id, status, stream_session_id, stream_status ON request_logs
FOR EACH ROW
BEGIN
  SELECT CASE WHEN (NEW.stream_session_id IS NULL)<>(NEW.stream_status IS NULL)
    THEN RAISE(ABORT, 'request_log_stream_fields_incomplete') END;
  SELECT CASE WHEN NEW.stream_session_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM stream_sessions ss
    WHERE ss.stream_session_id=NEW.stream_session_id
      AND ss.tenant_id=NEW.tenant_id
      AND ss.request_id=NEW.request_id
      AND ss.status=NEW.stream_status
  ) THEN RAISE(ABORT, 'request_log_stream_session_mismatch') END;
  SELECT CASE WHEN NEW.stream_status IS NOT NULL AND NOT (
    (NEW.stream_status='completed' AND NEW.status='success')
    OR (NEW.stream_status<>'completed' AND NEW.status='failed')
  ) THEN RAISE(ABORT, 'request_log_stream_status_mismatch') END;
END;

-- request_logs 复合主键稳定后恢复价格快照及其三个既有索引。
ALTER TABLE price_snapshots_prd045_new RENAME TO price_snapshots;
CREATE INDEX idx_price_snapshots_request_id ON price_snapshots(request_id);
CREATE INDEX idx_price_snapshots_tenant_time
  ON price_snapshots(tenant_id, created_at DESC);
CREATE INDEX idx_price_snapshots_price_plan
  ON price_snapshots(tenant_id, price_plan_id, created_at DESC);

-- Usage 表在 request_logs 复合主键稳定后恢复正式名称、十个索引和四条既有触发器。
ALTER TABLE usage_ledger_prd045_new RENAME TO usage_ledger;
CREATE INDEX idx_usage_ledger_tenant_id ON usage_ledger(tenant_id);
CREATE INDEX idx_usage_ledger_request_id ON usage_ledger(request_id);
CREATE INDEX idx_usage_ledger_tenant_time
  ON usage_ledger(tenant_id, created_at DESC);
CREATE INDEX idx_usage_ledger_model_time
  ON usage_ledger(tenant_id, model_id, created_at DESC);
CREATE INDEX idx_usage_ledger_virtual_key_time
  ON usage_ledger(tenant_id, virtual_key_id, created_at DESC);
CREATE INDEX idx_usage_ledger_estimated_time
  ON usage_ledger(tenant_id, estimated, created_at DESC);
CREATE INDEX idx_usage_ledger_project_time
  ON usage_ledger(tenant_id, project_id, created_at DESC);
CREATE INDEX idx_usage_ledger_application_time
  ON usage_ledger(tenant_id, application_id, created_at DESC);
CREATE INDEX idx_usage_ledger_environment_time
  ON usage_ledger(tenant_id, environment_id, created_at DESC);
CREATE INDEX idx_usage_ledger_routing_attempt
  ON usage_ledger(tenant_id, routing_attempt_id);

CREATE TRIGGER trg_usage_routing_attempt_insert
BEFORE INSERT ON usage_ledger
FOR EACH ROW
WHEN NEW.routing_attempt_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM routing_attempts ra
    WHERE ra.routing_attempt_id=NEW.routing_attempt_id
      AND ra.tenant_id=NEW.tenant_id
      AND ra.request_id=NEW.request_id
      AND ra.status='succeeded'
  ) THEN RAISE(ABORT, 'usage_routing_attempt_mismatch') END;
END;

CREATE TRIGGER trg_usage_ledger_export_append_only_update
BEFORE UPDATE ON usage_ledger
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_ledger_append_only');
END;

CREATE TRIGGER trg_usage_ledger_export_append_only_delete
BEFORE DELETE ON usage_ledger
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_ledger_append_only');
END;

CREATE TRIGGER trg_usage_ledger_export_sequence_insert
AFTER INSERT ON usage_ledger
FOR EACH ROW
BEGIN
  -- 新 Usage 与导出序列保持同事务追加，避免导出水位漏记账本。
  INSERT INTO usage_export_ledger_sequences(tenant_id, usage_ledger_id)
  VALUES(NEW.tenant_id, NEW.usage_ledger_id);
END;

-- sequence 自身仍是不可变映射；重建结束后逐字恢复 owner 与两条追加保护。
CREATE TRIGGER trg_usage_export_ledger_sequences_owner_insert
BEFORE INSERT ON usage_export_ledger_sequences
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM usage_ledger ul
  WHERE ul.usage_ledger_id = NEW.usage_ledger_id AND ul.tenant_id = NEW.tenant_id
)
BEGIN
  SELECT RAISE(ABORT, 'usage_export_ledger_sequence_owner_mismatch');
END;

CREATE TRIGGER trg_usage_export_ledger_sequences_immutable_update
BEFORE UPDATE ON usage_export_ledger_sequences
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_export_ledger_sequence_immutable');
END;

CREATE TRIGGER trg_usage_export_ledger_sequences_immutable_delete
BEFORE DELETE ON usage_export_ledger_sequences
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_export_ledger_sequence_immutable');
END;

-- claim 在读取请求正文前提交，只保存可信身份和快照租约，不保存请求或响应正文。
CREATE TABLE gateway_request_claims (
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
  virtual_key_id TEXT,
  snapshot_version INTEGER CHECK (
    snapshot_version IS NULL
    OR (typeof(snapshot_version) = 'integer' AND snapshot_version >= 1)
  ),
  policy_snapshot_status TEXT NOT NULL CHECK (
    policy_snapshot_status IN (
      'disabled', 'not_published', 'current', 'lkg',
      'emergency_lkg', 'expired', 'unavailable'
    )
  ),
  claimed_at TEXT NOT NULL CHECK (length(claimed_at) BETWEEN 1 AND 64),
  PRIMARY KEY (tenant_id, request_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, virtual_key_id)
    REFERENCES virtual_keys(tenant_id, virtual_key_id),
  FOREIGN KEY (tenant_id, snapshot_version)
    REFERENCES policy_snapshots(tenant_id, snapshot_version),
  CHECK (
    (snapshot_version IS NOT NULL
      AND policy_snapshot_status IN ('current', 'lkg', 'emergency_lkg', 'expired'))
    OR (snapshot_version IS NULL
      AND policy_snapshot_status IN (
        'disabled', 'not_published', 'lkg', 'emergency_lkg', 'expired', 'unavailable'
      ))
  )
);

CREATE INDEX idx_gateway_request_claims_tenant_time
  ON gateway_request_claims(tenant_id, claimed_at DESC, request_id DESC);

-- 历史日志发生在快照能力上线前，统一标记 disabled 并逐行生成同租户 claim。
INSERT INTO gateway_request_claims(
  tenant_id, request_id, trace_id, virtual_key_id,
  snapshot_version, policy_snapshot_status, claimed_at
)
SELECT
  tenant_id, request_id, trace_id, virtual_key_id,
  NULL, 'disabled', COALESCE(created_at, updated_at, datetime('now'))
FROM request_logs;

CREATE TRIGGER trg_gateway_request_claims_snapshot_insert
BEFORE INSERT ON gateway_request_claims
FOR EACH ROW
WHEN NEW.snapshot_version IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM policy_snapshots snapshot
  WHERE snapshot.tenant_id = NEW.tenant_id
    AND snapshot.snapshot_version = NEW.snapshot_version
    AND snapshot.state = 'published'
)
BEGIN
  SELECT RAISE(ABORT, 'request_claim_snapshot_mismatch');
END;

CREATE TRIGGER trg_gateway_request_claims_immutable_update
BEFORE UPDATE ON gateway_request_claims
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_claim_immutable');
END;

CREATE TRIGGER trg_gateway_request_claims_immutable_delete
BEFORE DELETE ON gateway_request_claims
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_claim_immutable');
END;

-- request_logs 只能写入已经 claim 的同租户、同链路、同 Key 和同快照租约。
CREATE TRIGGER trg_request_logs_claim_insert
BEFORE INSERT ON request_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM gateway_request_claims claim
  WHERE claim.tenant_id = NEW.tenant_id
    AND claim.request_id = NEW.request_id
    AND claim.trace_id = NEW.trace_id
    AND claim.virtual_key_id IS NEW.virtual_key_id
    AND claim.snapshot_version IS NEW.snapshot_version
    AND claim.policy_snapshot_status = NEW.policy_snapshot_status
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_claim_mismatch');
END;

CREATE TRIGGER trg_request_logs_claim_update
BEFORE UPDATE OF tenant_id, request_id, trace_id, virtual_key_id,
  snapshot_version, policy_snapshot_status
ON request_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM gateway_request_claims claim
  WHERE claim.tenant_id = NEW.tenant_id
    AND claim.request_id = NEW.request_id
    AND claim.trace_id = NEW.trace_id
    AND claim.virtual_key_id IS NEW.virtual_key_id
    AND claim.snapshot_version IS NEW.snapshot_version
    AND claim.policy_snapshot_status = NEW.policy_snapshot_status
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_claim_mismatch');
END;

-- 非空版本还必须归属于同租户已发布快照；claim 校验之外保留显式诊断防线。
CREATE TRIGGER trg_request_logs_snapshot_insert
BEFORE INSERT ON request_logs
FOR EACH ROW
WHEN NEW.snapshot_version IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM policy_snapshots snapshot
  WHERE snapshot.tenant_id = NEW.tenant_id
    AND snapshot.snapshot_version = NEW.snapshot_version
    AND snapshot.state = 'published'
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_snapshot_mismatch');
END;

CREATE TRIGGER trg_request_logs_snapshot_update
BEFORE UPDATE OF tenant_id, snapshot_version ON request_logs
FOR EACH ROW
WHEN NEW.snapshot_version IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM policy_snapshots snapshot
  WHERE snapshot.tenant_id = NEW.tenant_id
    AND snapshot.snapshot_version = NEW.snapshot_version
    AND snapshot.state = 'published'
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_snapshot_mismatch');
END;

-- audit_logs 带动作/对象 CHECK，必须按 0033 全字段重建并逐字段保留历史事实。
DROP TABLE IF EXISTS audit_logs_prd045_new;
CREATE TABLE audit_logs_prd045_new (
  audit_log_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (actor_role IN ('admin', 'developer', 'viewer')),
  action TEXT NOT NULL CHECK (
    action IN (
      'provider.created', 'provider.updated',
      'provider_key.created', 'provider_key.updated',
      'virtual_key.created', 'virtual_key.updated',
      'budget_rule.created', 'budget_rule.updated',
      'dlp_rule.created', 'dlp_rule.updated',
      'webhook_delivery.replayed', 'webhook_endpoint.secret_rotated',
      'usage_export.requested', 'usage_export.downloaded',
      'policy.created', 'policy.updated',
      'policy_snapshot.published', 'policy_snapshot.rolled_back'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot'
    )
  ),
  object_id TEXT NOT NULL,
  object_name TEXT,
  before_summary_json TEXT NOT NULL CHECK (
    json_valid(before_summary_json) AND json_type(before_summary_json) = 'object'
  ),
  after_summary_json TEXT NOT NULL CHECK (
    json_valid(after_summary_json) AND json_type(after_summary_json) = 'object'
  ),
  changed_fields_json TEXT NOT NULL CHECK (
    json_valid(changed_fields_json) AND json_type(changed_fields_json) = 'array'
  ),
  client_ip TEXT,
  user_agent TEXT,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

-- 全量复制 0033 审计事实；身份快照、摘要、来源和链路字段必须原样保留。
INSERT INTO audit_logs_prd045_new(
  audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
)
SELECT
  audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
FROM audit_logs;
DROP TABLE audit_logs;
-- 复制完成后恢复稳定表名，随后逐一重建既有索引和安全触发器。
ALTER TABLE audit_logs_prd045_new RENAME TO audit_logs;

CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id, created_at DESC, audit_log_id DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id, actor_user_id, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id, object_type, object_id, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id, action, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id, request_id);

CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users u
  WHERE u.user_id = NEW.actor_user_id AND u.tenant_id = NEW.tenant_id
)
BEGIN
  SELECT RAISE(ABORT, 'audit_log_owner_mismatch');
END;

CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;

CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;

COMMIT;
PRAGMA foreign_keys = ON;
