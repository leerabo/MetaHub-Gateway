-- 迁移职责：为 PRD-042 增加 Webhook 详情重放、幂等键和签名 Secret 版本追踪。
-- delivery 表存在 attempts 子表和自关联字段，重建期间必须暂时关闭外键，提交后立即恢复。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- Endpoint 只保存当前 Secret 的密文和不可逆摘要；版本字段用于确认某次投递使用的签名代次。
ALTER TABLE webhook_endpoints ADD COLUMN secret_version INTEGER NOT NULL DEFAULT 1 CHECK (secret_version >= 1);
-- 初始创建不属于轮换，因此历史 Endpoint 的 rotated_at 保持 NULL。
ALTER TABLE webhook_endpoints ADD COLUMN secret_rotated_at TEXT;
-- 轮换操作者由认证上下文写入，不能接受前端提交的用户 ID。
ALTER TABLE webhook_endpoints ADD COLUMN secret_rotated_by TEXT REFERENCES users(user_id);

-- 清理异常执行可能遗留的临时表，所有操作都在同一事务内完成。
DROP TABLE IF EXISTS webhook_deliveries_prd042_new;

-- 重建 delivery 表：原始事件继续按 event_id 幂等，手工重放改用来源 delivery + 幂等键约束。
CREATE TABLE webhook_deliveries_prd042_new (
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
  delivery_kind TEXT NOT NULL DEFAULT 'original' CHECK (delivery_kind IN ('original', 'replay')),
  replayed_from_delivery_id TEXT,
  replay_idempotency_key TEXT,
  replayed_by TEXT,
  replayed_at TEXT,
  secret_version INTEGER NOT NULL DEFAULT 1 CHECK (secret_version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (webhook_endpoint_id) REFERENCES webhook_endpoints(webhook_endpoint_id),
  FOREIGN KEY (replayed_from_delivery_id) REFERENCES webhook_deliveries_prd042_new(webhook_delivery_id),
  FOREIGN KEY (replayed_by) REFERENCES users(user_id),
  CHECK (
    (delivery_kind = 'original' AND replayed_from_delivery_id IS NULL
      AND replay_idempotency_key IS NULL AND replayed_by IS NULL AND replayed_at IS NULL)
    OR
    (delivery_kind = 'replay' AND replayed_from_delivery_id IS NOT NULL
      AND replay_idempotency_key IS NOT NULL AND replayed_by IS NOT NULL AND replayed_at IS NOT NULL)
  )
);

-- 历史 delivery 全部来自 PRD-025 原始投递，迁移时保持业务字段和时间字段不变。
INSERT INTO webhook_deliveries_prd042_new(
  webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
  event_timestamp, payload_json, status, attempt_count, max_attempts,
  http_status, duration_ms, error_code, error_summary, next_retry_at,
  delivery_kind, replayed_from_delivery_id, replay_idempotency_key,
  replayed_by, replayed_at, secret_version, created_at, updated_at
)
SELECT
  webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
  event_timestamp, payload_json, status, attempt_count, max_attempts,
  http_status, duration_ms, error_code, error_summary, next_retry_at,
  'original', NULL, NULL, NULL, NULL, 1, created_at, updated_at
FROM webhook_deliveries;

-- 表替换必须与索引恢复处于同一事务，应用层不会看到半完成结构。
DROP TABLE webhook_deliveries;
-- 临时表包含完整新约束，重命名后恢复稳定业务表名供应用无感升级。
ALTER TABLE webhook_deliveries_prd042_new RENAME TO webhook_deliveries;

-- 原始事件仍以稳定 event_id 去重，保持 shared runtime 的 INSERT OR IGNORE 语义。
CREATE UNIQUE INDEX idx_webhook_deliveries_original_event_unique
  ON webhook_deliveries(tenant_id, webhook_endpoint_id, event_id)
  WHERE delivery_kind = 'original';

-- 同一来源 delivery 和幂等键只允许创建一个 replay，避免并发请求重复出网。
CREATE UNIQUE INDEX idx_webhook_deliveries_replay_idempotency_unique
  ON webhook_deliveries(tenant_id, webhook_endpoint_id, replayed_from_delivery_id, replay_idempotency_key)
  WHERE delivery_kind = 'replay';

-- 恢复 PRD-025 的查询索引，并增加详情页稳定排序和 replay 链路查询索引。
CREATE INDEX idx_webhook_deliveries_endpoint_time
  ON webhook_deliveries(tenant_id, webhook_endpoint_id, created_at DESC);
CREATE INDEX idx_webhook_deliveries_event
  ON webhook_deliveries(tenant_id, event_id);
CREATE INDEX idx_webhook_deliveries_status_time
  ON webhook_deliveries(tenant_id, status, created_at DESC);
CREATE INDEX idx_webhook_deliveries_replay_source
  ON webhook_deliveries(tenant_id, replayed_from_delivery_id, created_at DESC);

-- 数据库第二道租户边界：Endpoint 必须属于 delivery tenant，replay 来源和操作者还必须同租户同 Endpoint。
CREATE TRIGGER trg_webhook_deliveries_prd042_owner_insert
BEFORE INSERT ON webhook_deliveries
FOR EACH ROW
WHEN
  NOT EXISTS (
    SELECT 1 FROM webhook_endpoints e
    WHERE e.webhook_endpoint_id=NEW.webhook_endpoint_id AND e.tenant_id=NEW.tenant_id
  )
  OR (
    NEW.delivery_kind='replay' AND (
      NOT EXISTS (
        SELECT 1 FROM webhook_deliveries source
        WHERE source.webhook_delivery_id=NEW.replayed_from_delivery_id
          AND source.tenant_id=NEW.tenant_id
          AND source.webhook_endpoint_id=NEW.webhook_endpoint_id
      )
      OR NOT EXISTS (
        SELECT 1 FROM users u
        WHERE u.user_id=NEW.replayed_by AND u.tenant_id=NEW.tenant_id
      )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'webhook_delivery_owner_mismatch');
END;

-- replay 归属字段创建后不可改写，状态更新不能把历史记录重新挂到其他租户、Endpoint 或操作者。
CREATE TRIGGER trg_webhook_deliveries_prd042_owner_immutable
BEFORE UPDATE OF tenant_id, webhook_endpoint_id, delivery_kind,
  replayed_from_delivery_id, replay_idempotency_key, replayed_by, replayed_at
ON webhook_deliveries
FOR EACH ROW
WHEN
  NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.webhook_endpoint_id IS NOT OLD.webhook_endpoint_id
  OR NEW.delivery_kind IS NOT OLD.delivery_kind
  OR NEW.replayed_from_delivery_id IS NOT OLD.replayed_from_delivery_id
  OR NEW.replay_idempotency_key IS NOT OLD.replay_idempotency_key
  OR NEW.replayed_by IS NOT OLD.replayed_by
  OR NEW.replayed_at IS NOT OLD.replayed_at
BEGIN
  SELECT RAISE(ABORT, 'webhook_delivery_owner_immutable');
END;

-- Secret 轮换操作者必须属于 Endpoint tenant，不能只依赖 user_id 外键。
CREATE TRIGGER trg_webhook_endpoints_prd042_rotator_owner
BEFORE UPDATE OF secret_rotated_by ON webhook_endpoints
FOR EACH ROW
WHEN NEW.secret_rotated_by IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM users u
  WHERE u.user_id=NEW.secret_rotated_by AND u.tenant_id=NEW.tenant_id
)
BEGIN
  SELECT RAISE(ABORT, 'webhook_secret_rotator_owner_mismatch');
END;

-- PRD-034 审计表同时有数据库动作/对象 CHECK；应用白名单扩展必须与数据库约束原子升级。
DROP TABLE IF EXISTS audit_logs_prd042_new;
CREATE TABLE audit_logs_prd042_new (
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
      'webhook_delivery.replayed', 'webhook_endpoint.secret_rotated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint'
    )
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

-- 复制全部既有管理审计事实，升级不能改变历史身份快照、摘要或请求链路字段。
INSERT INTO audit_logs_prd042_new(
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

-- 表替换后必须完整恢复查询索引、租户所有权和不可变触发器，审计安全边界不能短暂缺失。
DROP TABLE audit_logs;
-- 临时表已经包含扩展后的 CHECK，重命名后恢复稳定审计表名。
ALTER TABLE audit_logs_prd042_new RENAME TO audit_logs;
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
