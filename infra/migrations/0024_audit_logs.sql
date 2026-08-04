-- 迁移说明：PRD-034 新增租户级管理操作审计日志。该表只允许追加，且不保存密钥、Token、Prompt 或 DLP 原始 pattern。

-- 审计记录保存操作发生时的身份快照与脱敏变更摘要，不依赖用户后续是否改名。
CREATE TABLE IF NOT EXISTS audit_logs (
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

-- 默认列表按租户和时间稳定倒序，audit_log_id 用于同秒记录的确定性排序。
CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_time
  ON audit_logs(tenant_id, created_at DESC, audit_log_id DESC);
-- 操作者筛选仍先限定 tenant_id，禁止通过用户 ID 探测其他租户记录。
CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id, actor_user_id, created_at DESC);
-- 对象追溯同时绑定类型和 ID，避免不同资源 ID 空间相互干扰。
CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id, object_type, object_id, created_at DESC);
-- 动作筛选用于安全负责人快速定位同类管理变更。
CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id, action, created_at DESC);
-- request_id 索引用于把 API 响应、应用日志和审计事实串联起来。
CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_request
  ON audit_logs(tenant_id, request_id);

-- INSERT 时校验操作者当前属于同一租户，防止业务代码拼接出跨租户审计事实。
CREATE TRIGGER IF NOT EXISTS trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users u
  WHERE u.user_id=NEW.actor_user_id AND u.tenant_id=NEW.tenant_id
)
BEGIN
  SELECT RAISE(ABORT, 'audit_log_owner_mismatch');
END;

-- 审计记录是追加事实，普通业务连接不得修改历史内容。
CREATE TRIGGER IF NOT EXISTS trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;

-- 审计记录不提供单行删除能力；保留期清理需由后续受控归档方案实现。
CREATE TRIGGER IF NOT EXISTS trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;
