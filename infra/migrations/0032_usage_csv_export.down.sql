-- 回滚职责：移除 PRD-043 Usage CSV 制品、完成事件和导出审计动作，恢复完整 0031 审计合同。
-- 回滚会丢弃 PRD-043 专属任务和审计，但不得修改 Usage Ledger、价格快照或此前管理审计。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 先重建审计表并过滤 PRD-043 动作；不可变 DELETE 触发器不会阻断表到表的受控复制。
DROP TABLE IF EXISTS audit_logs_prd043_old;
CREATE TABLE audit_logs_prd043_old (
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

-- 只排除本 PRD 两类导出审计，0031 Webhook 和所有更早管理事实逐字段原样保留。
INSERT INTO audit_logs_prd043_old(
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
WHERE action NOT IN ('usage_export.requested', 'usage_export.downloaded');

DROP TABLE audit_logs;
-- 恢复 0031 稳定审计表名，使旧版本服务继续使用原路由和查询合同。
ALTER TABLE audit_logs_prd043_old RENAME TO audit_logs;
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

-- 事件引用任务，必须先移除事件及其不可变触发器，再删除任务表。
DROP TRIGGER IF EXISTS trg_usage_export_events_immutable_delete;
DROP TRIGGER IF EXISTS trg_usage_export_events_immutable_update;
DROP TRIGGER IF EXISTS trg_usage_export_events_owner_insert;
-- 完成事件完全属于 PRD-043，回滚时随任务能力整体移除。
DROP TABLE IF EXISTS usage_export_events;
DROP TRIGGER IF EXISTS trg_usage_export_jobs_status_transition;
DROP TRIGGER IF EXISTS trg_usage_export_jobs_identity_immutable;
DROP TRIGGER IF EXISTS trg_usage_export_jobs_owner_insert;
-- 最后删除任务及 BLOB，保证删除前已没有事件外键引用。
DROP TABLE IF EXISTS usage_export_jobs;

-- 先解除 Usage Ledger 的运行期追加保护，再删除序列触发器和映射表，恢复 0031 写入合同。
DROP TRIGGER IF EXISTS trg_usage_ledger_export_sequence_insert;
DROP TRIGGER IF EXISTS trg_usage_ledger_export_append_only_delete;
DROP TRIGGER IF EXISTS trg_usage_ledger_export_append_only_update;
DROP TRIGGER IF EXISTS trg_usage_export_ledger_sequences_immutable_delete;
DROP TRIGGER IF EXISTS trg_usage_export_ledger_sequences_immutable_update;
DROP TRIGGER IF EXISTS trg_usage_export_ledger_sequences_owner_insert;
-- 回滚最后删除 PRD-043 专属序列映射，0031 及更早版本不依赖该表。
DROP TABLE IF EXISTS usage_export_ledger_sequences;

COMMIT;
PRAGMA foreign_keys = ON;
