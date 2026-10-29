-- 回滚职责：移除 PRD-044 策略 DSL 表和 policy 审计动作，恢复完整 0032 审计合同。
-- 数据边界：回滚会丢弃 Policy/Rule/Action/Event，但 Usage 导出、Webhook 和全部更早审计必须逐字段保留。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- audit_logs 有不可变 DELETE 触发器，不能直接删除 policy 行；先建 0032 旧结构再过滤复制。
DROP TABLE IF EXISTS audit_logs_prd044_old;
CREATE TABLE audit_logs_prd044_old (
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
      'usage_export.requested', 'usage_export.downloaded'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job'
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

-- 仅过滤 PRD-044 的两类审计；0032 的 14 个动作、8 个对象和全部 17 个字段原样复制。
INSERT INTO audit_logs_prd044_old(
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
WHERE action NOT IN ('policy.created', 'policy.updated');

DROP TABLE audit_logs;
-- 恢复 0032 稳定表名、五个索引与三条安全触发器，后续 0032 down 可继续无损执行。
ALTER TABLE audit_logs_prd044_old RENAME TO audit_logs;
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

-- Event 引用 Policy，必须先解除追加保护并删除事件表，不能先删父对象。
DROP TRIGGER IF EXISTS trg_policy_events_immutable_delete;
DROP TRIGGER IF EXISTS trg_policy_events_immutable_update;
DROP TRIGGER IF EXISTS trg_policy_events_owner_insert;
-- 删除策略事件表：PRD-044 之前不存在 created/validation_failed 策略事件账本。
DROP TABLE IF EXISTS policy_events;

-- Action 同时引用 Policy 和 Model；事件移除后先删 Action，释放两类外键关系。
DROP TRIGGER IF EXISTS trg_policy_actions_identity_immutable;
DROP TRIGGER IF EXISTS trg_policy_actions_target_active_update;
DROP TRIGGER IF EXISTS trg_policy_actions_target_active_insert;
DROP TRIGGER IF EXISTS trg_policy_actions_owner_update;
DROP TRIGGER IF EXISTS trg_policy_actions_owner_insert;
-- 删除策略动作表：回滚后 route_to 等受限 DSL 动作不再持久化。
DROP TABLE IF EXISTS policy_actions;

-- Rule 只引用 Policy，按子到父顺序删除并明确解除归属、身份触发器。
DROP TRIGGER IF EXISTS trg_policy_rules_identity_immutable;
DROP TRIGGER IF EXISTS trg_policy_rules_owner_update;
DROP TRIGGER IF EXISTS trg_policy_rules_owner_insert;
-- 删除策略规则表：DSL 正文、canonical AST 与 hash 均属于 PRD-044 专属数据。
DROP TABLE IF EXISTS policy_rules;

-- 所有子表已移除后再删除 Policy，避免未来在启用外键的手工回放中形成悬空引用。
DROP TRIGGER IF EXISTS trg_policies_version_increment;
DROP TRIGGER IF EXISTS trg_policies_identity_immutable;
DROP TRIGGER IF EXISTS trg_policies_owner_update;
DROP TRIGGER IF EXISTS trg_policies_owner_insert;
-- 删除策略主表：所有子表已经按外键依赖顺序清理完毕。
DROP TABLE IF EXISTS policies;

-- 复合父键只由 PRD-044 使用，必须最后删除；0032 及更早结构不依赖这两个索引。
DROP INDEX IF EXISTS uq_models_tenant_model_prd044;
-- users 复合唯一索引同样只服务策略 creator/actor 外键，最后恢复 0032 索引集合。
DROP INDEX IF EXISTS uq_users_tenant_user_prd044;

COMMIT;
PRAGMA foreign_keys = ON;
