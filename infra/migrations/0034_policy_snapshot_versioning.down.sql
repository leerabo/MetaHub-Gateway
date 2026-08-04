-- 回滚职责：移除 PRD-045 快照、claim、日志扩展与新审计，恢复完整 0033 数据合同。
-- 数据安全：先检测复合 request_id 能否降级；冲突时整段事务失败，不静默改 ID 或丢 Usage。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 0033 使用全局 request_id 主键；有效 0034 数据若跨租户重名，必须在任何清理前稳定失败。
DROP TABLE IF EXISTS policy_snapshot_down_request_guard;
CREATE TABLE policy_snapshot_down_request_guard (
  request_id TEXT PRIMARY KEY
);
-- UNIQUE guard 只做可逆性预检；任一重复都会让整个显式事务在清理前回滚。
INSERT INTO policy_snapshot_down_request_guard(request_id)
SELECT request_id FROM request_logs;
DROP TABLE policy_snapshot_down_request_guard;

-- 旧应用无法解释新 Webhook 类型：先删 attempts，再删原始/重放 delivery，不能文本匹配 payload。
DELETE FROM webhook_delivery_attempts
WHERE webhook_delivery_id IN (
  SELECT webhook_delivery_id
  FROM webhook_deliveries
  WHERE event_type = 'policy_snapshot.published'
);
-- attempts 已解除后再删除原始和重放摘要，避免留下旧应用无法解释的事件类型。
DELETE FROM webhook_deliveries
WHERE event_type = 'policy_snapshot.published';

-- Endpoint 订阅使用 SQLite JSON API结构化移除；空集合必须保持合法 JSON 数组 []。
UPDATE webhook_endpoints
SET event_types_json = COALESCE(
  (
    SELECT json_group_array(value)
    FROM (
      SELECT item.value AS value
      FROM json_each(webhook_endpoints.event_types_json) AS item
      WHERE item.value != 'policy_snapshot.published'
      ORDER BY item.key
    )
  ),
  '[]'
)
WHERE EXISTS (
  SELECT 1
  FROM json_each(webhook_endpoints.event_types_json) AS item
  WHERE item.value = 'policy_snapshot.published'
);

-- audit_logs 先恢复 0033 白名单，只过滤 PRD-045 的两类快照审计事实。
DROP TABLE IF EXISTS audit_logs_prd045_old;
CREATE TABLE audit_logs_prd045_old (
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
      'policy.created', 'policy.updated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy'
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

-- 只排除 PRD-045 动作，其余 0033 审计 17 列逐字段保留。
INSERT INTO audit_logs_prd045_old(
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
FROM audit_logs
WHERE action NOT IN ('policy_snapshot.published', 'policy_snapshot.rolled_back');
DROP TABLE audit_logs;
-- 过滤复制完成后恢复稳定审计表名，再恢复五索引与三触发器。
ALTER TABLE audit_logs_prd045_old RENAME TO audit_logs;

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

-- 与 up 相同，先解除所有外部 sequence 触发器，避免 usage_ledger 缺表窗口触发 SQLite 校验失败。
DROP TRIGGER IF EXISTS trg_usage_export_ledger_sequences_owner_insert;
DROP TRIGGER IF EXISTS trg_usage_export_ledger_sequences_immutable_update;
DROP TRIGGER IF EXISTS trg_usage_export_ledger_sequences_immutable_delete;

-- price_snapshots 同步恢复 0033 的 request_id 单列外键，全部业务字段逐列保留。
DROP TABLE IF EXISTS price_snapshots_prd045_old;
CREATE TABLE price_snapshots_prd045_old (
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
  FOREIGN KEY (request_id) REFERENCES request_logs(request_id),
  FOREIGN KEY (model_id) REFERENCES models(model_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id)
);
-- 价格快照所有历史字段原样复制，down 不重算模型价格或 Provider 摘要。
INSERT INTO price_snapshots_prd045_old(
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

-- 先复制 Usage；其临时表声明 0033 的全局 request_id FK，待 request_logs 恢复后再重命名。
DROP TABLE IF EXISTS usage_ledger_prd045_old;
CREATE TABLE usage_ledger_prd045_old (
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
  FOREIGN KEY (request_id) REFERENCES request_logs(request_id)
);

-- Usage 全部 32 列逐字段复制，等待 request_logs 全局主键恢复后再替换稳定表名。
INSERT INTO usage_ledger_prd045_old(
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

-- 恢复 0033 request_logs 的全部 43 列和全局 request_id 主键；PRD-045 摘要列按设计移除。
DROP TABLE IF EXISTS request_logs_prd045_old;
CREATE TABLE request_logs_prd045_old (
  request_id TEXT PRIMARY KEY,
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
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(virtual_key_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id),
  FOREIGN KEY (model_id) REFERENCES models(model_id)
);

-- 仅丢弃 PRD-045 新增摘要列；0033 的 43 列值逐字段恢复。
INSERT INTO request_logs_prd045_old(
  request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
  status, error_code, latency_ms, created_at, endpoint, request_model,
  resolved_model, provider_code, input_tokens, output_tokens, total_tokens,
  estimated_cost_amount, currency, price_schema_version, updated_at,
  budget_status, budget_rule_id, budget_scope_type, budget_scope_id,
  budget_action, budget_reason_code, budget_current_cost_cny, budget_limit_cny,
  dlp_status, dlp_rule_id, dlp_action, dlp_match_count, dlp_reason_code,
  project_id, application_id, environment_id, environment_code,
  fallback_triggered, routing_attempt_count, final_routing_attempt_id,
  stream_session_id, stream_status
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
  stream_session_id, stream_status
FROM request_logs;
DROP TABLE request_logs;
-- 唯一性 guard 已通过，因此此处可安全恢复全局 request_id 主键表。
ALTER TABLE request_logs_prd045_old RENAME TO request_logs;

-- 恢复 0033 的十五个 request_logs 索引和四条关联一致性触发器。
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

-- 恢复价格快照正式表名和 0033 的三个索引。
ALTER TABLE price_snapshots_prd045_old RENAME TO price_snapshots;
CREATE INDEX idx_price_snapshots_request_id ON price_snapshots(request_id);
CREATE INDEX idx_price_snapshots_tenant_time
  ON price_snapshots(tenant_id, created_at DESC);
CREATE INDEX idx_price_snapshots_price_plan
  ON price_snapshots(tenant_id, price_plan_id, created_at DESC);

-- request_logs 稳定后恢复 Usage 正式表名、十个索引和四条既有触发器。
ALTER TABLE usage_ledger_prd045_old RENAME TO usage_ledger;
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
  -- 恢复 0033 的 Usage 到导出序列同事务追加合同。
  INSERT INTO usage_export_ledger_sequences(tenant_id, usage_ledger_id)
  VALUES(NEW.tenant_id, NEW.usage_ledger_id);
END;

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

-- 日志新触发器已随表替换移除；claim 删除后才能撤销其 Virtual Key 复合候选键。
DROP TRIGGER IF EXISTS trg_gateway_request_claims_immutable_delete;
DROP TRIGGER IF EXISTS trg_gateway_request_claims_immutable_update;
DROP TRIGGER IF EXISTS trg_gateway_request_claims_snapshot_insert;
-- claim 是 PRD-045 专属占位事实，日志恢复旧结构后按设计整体移除。
DROP TABLE IF EXISTS gateway_request_claims;
-- claim 已删除后不再有复合 Virtual Key FK，最后撤销专属候选索引。
DROP INDEX IF EXISTS uq_virtual_keys_tenant_key_prd045;

-- 按外键逆序移除 event -> head -> item -> snapshot，并先解除各自不可变保护。
DROP TRIGGER IF EXISTS trg_policy_snapshot_events_immutable_delete;
DROP TRIGGER IF EXISTS trg_policy_snapshot_events_immutable_update;
DROP TRIGGER IF EXISTS trg_policy_snapshot_events_insert;
-- 事件最先删除，解除对 snapshot header 的三列复合外键。
DROP TABLE IF EXISTS policy_snapshot_events;

DROP TRIGGER IF EXISTS trg_policy_snapshot_heads_delete;
DROP TRIGGER IF EXISTS trg_policy_snapshot_heads_update;
DROP TRIGGER IF EXISTS trg_policy_snapshot_heads_insert;
-- head 删除后数据面不再有当前快照指针，也释放 header 引用。
DROP TABLE IF EXISTS policy_snapshot_heads;

DROP TRIGGER IF EXISTS trg_policy_snapshot_items_delete_guard;
DROP TRIGGER IF EXISTS trg_policy_snapshot_items_immutable_update;
DROP TRIGGER IF EXISTS trg_policy_snapshot_items_guard_insert;
DROP TRIGGER IF EXISTS trg_policy_snapshot_items_rollback_insert;
DROP TRIGGER IF EXISTS trg_policy_snapshot_items_publish_insert;
-- items 按子到父顺序删除，解除 Policy、Model 与 snapshot 三类引用。
DROP TABLE IF EXISTS policy_snapshot_items;

DROP TRIGGER IF EXISTS trg_policy_snapshots_delete_guard;
DROP TRIGGER IF EXISTS trg_policy_snapshots_seal_update;
DROP TRIGGER IF EXISTS trg_policy_snapshots_sequence_insert;
-- 所有子对象已移除后才能删除不可变 header，完成 PRD-045 数据回退。
DROP TABLE IF EXISTS policy_snapshots;

COMMIT;
PRAGMA foreign_keys = ON;
