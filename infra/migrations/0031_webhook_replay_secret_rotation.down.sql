-- 回滚职责：移除 PRD-042 的 replay/Secret 版本字段，恢复 PRD-025 Webhook 表结构。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- replay attempts 只属于 PRD-042；必须先删子记录，避免恢复旧唯一约束时留下悬空引用。
DELETE FROM webhook_delivery_attempts
WHERE webhook_delivery_id IN (
  SELECT webhook_delivery_id FROM webhook_deliveries WHERE delivery_kind = 'replay'
);

-- 清理异常回滚遗留的旧结构临时表，避免名称冲突阻断本次原子回滚。
DROP TABLE IF EXISTS webhook_deliveries_prd042_old;

-- 恢复 PRD-025 delivery 结构；回滚只复制 original，replay 数据随功能一起移除。
CREATE TABLE webhook_deliveries_prd042_old (
  webhook_delivery_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  webhook_endpoint_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_timestamp TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'success', 'failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1 AND max_attempts <= 3),
  http_status INTEGER,
  duration_ms INTEGER,
  error_code TEXT,
  error_summary TEXT,
  next_retry_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (webhook_endpoint_id) REFERENCES webhook_endpoints(webhook_endpoint_id),
  UNIQUE (tenant_id, webhook_endpoint_id, event_id)
);

-- 只复制 original；replay 是本 PRD 新数据，旧唯一约束无法且不应承载它。
INSERT INTO webhook_deliveries_prd042_old(
  webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
  event_timestamp, payload_json, status, attempt_count, max_attempts,
  http_status, duration_ms, error_code, error_summary, next_retry_at,
  created_at, updated_at
)
SELECT
  webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
  event_timestamp, payload_json, status, attempt_count, max_attempts,
  http_status, duration_ms, error_code, error_summary, next_retry_at,
  created_at, updated_at
FROM webhook_deliveries
WHERE delivery_kind = 'original';

DROP TABLE webhook_deliveries;
-- 恢复稳定 delivery 表名，使 PRD-025 服务代码可继续运行。
ALTER TABLE webhook_deliveries_prd042_old RENAME TO webhook_deliveries;

-- 恢复 PRD-025 delivery 索引，回滚后列表、事件回溯和失败筛选性能不退化。
CREATE INDEX idx_webhook_deliveries_endpoint_time
  ON webhook_deliveries(tenant_id, webhook_endpoint_id, created_at DESC);
CREATE INDEX idx_webhook_deliveries_event
  ON webhook_deliveries(tenant_id, event_id);
CREATE INDEX idx_webhook_deliveries_status_time
  ON webhook_deliveries(tenant_id, status, created_at DESC);

-- SQLite DROP COLUMN 对带外键和 CHECK 的历史结构兼容性有限，因此原子重建 Endpoint 表。
DROP TABLE IF EXISTS webhook_endpoints_prd042_old;
CREATE TABLE webhook_endpoints_prd042_old (
  webhook_endpoint_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL,
  endpoint_url TEXT NOT NULL,
  event_types_json TEXT NOT NULL,
  encrypted_secret TEXT NOT NULL,
  secret_prefix TEXT NOT NULL,
  secret_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  timeout_ms INTEGER NOT NULL DEFAULT 5000 CHECK (timeout_ms >= 1000 AND timeout_ms <= 30000),
  max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1 AND max_attempts <= 3),
  last_delivery_status TEXT,
  last_delivery_at TEXT,
  last_success_at TEXT,
  last_failure_at TEXT,
  created_by TEXT,
  updated_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (created_by) REFERENCES users(user_id),
  FOREIGN KEY (updated_by) REFERENCES users(user_id)
);

-- 复制 PRD-025 全部 Endpoint 字段，Secret 密文和历史状态必须原样保留。
INSERT INTO webhook_endpoints_prd042_old(
  webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
  encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
  max_attempts, last_delivery_status, last_delivery_at, last_success_at,
  last_failure_at, created_by, updated_by, created_at, updated_at
)
SELECT
  webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
  encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
  max_attempts, last_delivery_status, last_delivery_at, last_success_at,
  last_failure_at, created_by, updated_by, created_at, updated_at
FROM webhook_endpoints;

DROP TABLE webhook_endpoints;
-- 恢复稳定 Endpoint 表名，移除的仅是 PRD-042 轮换元数据。
ALTER TABLE webhook_endpoints_prd042_old RENAME TO webhook_endpoints;

-- 恢复 Endpoint 查询和 Secret 指纹排障索引。
CREATE INDEX idx_webhook_endpoints_tenant_status
  ON webhook_endpoints(tenant_id, status);
CREATE INDEX idx_webhook_endpoints_tenant_secret
  ON webhook_endpoints(tenant_id, secret_fingerprint);

-- 回滚审计动作/对象约束；仅移除 PRD-042 自身审计，其他历史管理事实必须完整保留。
DROP TABLE IF EXISTS audit_logs_prd042_old;
CREATE TABLE audit_logs_prd042_old (
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
      'dlp_rule.created', 'dlp_rule.updated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN ('provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule')
  ),
  object_id TEXT NOT NULL,
  object_name TEXT,
  before_summary_json TEXT NOT NULL CHECK (
    json_valid(before_summary_json) AND json_type(before_summary_json)='object'
  ),
  after_summary_json TEXT NOT NULL CHECK (
    json_valid(after_summary_json) AND json_type(after_summary_json)='object'
  ),
  changed_fields_json TEXT NOT NULL CHECK (
    json_valid(changed_fields_json) AND json_type(changed_fields_json)='array'
  ),
  client_ip TEXT,
  user_agent TEXT,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

-- PRD-042 audit 与 replay/rotation 元数据一起回滚；其他动作逐字段原样复制。
INSERT INTO audit_logs_prd042_old(
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
WHERE action NOT IN ('webhook_delivery.replayed', 'webhook_endpoint.secret_rotated');

-- 恢复 PRD-034 稳定表名、全部索引和不可变触发器。
DROP TABLE audit_logs;
-- 旧约束临时表重命名后，PRD-034 查询与审计服务无需感知回滚实现。
ALTER TABLE audit_logs_prd042_old RENAME TO audit_logs;
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
  WHERE u.user_id=NEW.actor_user_id AND u.tenant_id=NEW.tenant_id
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
