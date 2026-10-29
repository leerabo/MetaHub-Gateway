-- 回滚职责：精确移除 PRD-046 的预演事实，恢复完整 0034 Policy 事件与审计结构。
-- 数据安全：事件和审计受不可变触发器保护，必须过滤重建，不能直接 DELETE 或丢失旧事实。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 旧应用无法解释 Dry-Run Webhook：先清理子 attempts，再清理原始或重放 delivery。
DELETE FROM webhook_delivery_attempts
WHERE webhook_delivery_id IN (
  SELECT webhook_delivery_id
  FROM webhook_deliveries
  WHERE event_type = 'policy.dry_run_executed'
);
-- 子 attempts 清理后再移除目标 delivery，避免外键悬挂且不影响其他租户事件类型。
DELETE FROM webhook_deliveries
WHERE event_type = 'policy.dry_run_executed';

-- Endpoint 订阅必须通过 SQLite JSON API 精确移除目标值；其他类型、原顺序和合法空数组保留。
UPDATE webhook_endpoints
SET event_types_json = COALESCE(
  (
    SELECT json_group_array(value)
    FROM (
      SELECT item.value AS value
      FROM json_each(webhook_endpoints.event_types_json) AS item
      WHERE item.value != 'policy.dry_run_executed'
      ORDER BY item.key
    )
  ),
  '[]'
)
WHERE EXISTS (
  SELECT 1
  FROM json_each(webhook_endpoints.event_types_json) AS item
  WHERE item.value = 'policy.dry_run_executed'
);

-- audit_logs 恢复 0034 的动作/对象枚举，并只过滤 PRD-046 的 Dry-Run 动作事实。
DROP TABLE IF EXISTS audit_logs_prd046_old;
CREATE TABLE audit_logs_prd046_old (
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

-- 回滚只过滤 PRD-046 动作，其他租户的不可变审计事实按原列原值完整复制。
INSERT INTO audit_logs_prd046_old(
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
WHERE action != 'policy.dry_run_executed';

-- 核对复制数等于旧事实总数减去 Dry-Run 行数，防止过滤条件或列复制悄然丢数据。
DROP TABLE IF EXISTS audit_logs_prd046_down_guard;
CREATE TABLE audit_logs_prd046_down_guard (
  expected_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (expected_count = copied_count)
);
-- 过滤后的期望数量必须等于目标表数量，差异会在替换原表前回滚事务。
INSERT INTO audit_logs_prd046_down_guard(expected_count, copied_count)
SELECT
  (SELECT COUNT(*) FROM audit_logs WHERE action != 'policy.dry_run_executed'),
  (SELECT COUNT(*) FROM audit_logs_prd046_old);
DROP TABLE audit_logs_prd046_down_guard;

DROP TABLE audit_logs;
-- 通过计数守卫后才恢复 0034 表名，保证旧应用只看到完整且租户归属不变的审计集合。
ALTER TABLE audit_logs_prd046_old RENAME TO audit_logs;

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

-- policy_events 恢复 0033/0034 的两种事件语义，只过滤新的 Dry-Run 事件。
DROP TABLE IF EXISTS policy_events_prd046_old;
CREATE TABLE policy_events_prd046_old (
  policy_event_id TEXT PRIMARY KEY CHECK (
    length(policy_event_id) BETWEEN 6 AND 128 AND policy_event_id GLOB 'pevt_*'
  ),
  tenant_id TEXT NOT NULL,
  policy_id TEXT,
  event_type TEXT NOT NULL CHECK (
    event_type IN ('policy.created', 'policy.validation_failed')
  ),
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
  reason_code TEXT NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 128),
  payload_json TEXT NOT NULL CHECK (
    json_valid(payload_json)
    AND json_type(payload_json) = 'object'
    AND length(CAST(payload_json AS BLOB)) <= 8192
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, policy_id) REFERENCES policies(tenant_id, policy_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  CHECK (
    (event_type = 'policy.created'
      AND policy_id IS NOT NULL
      AND reason_code = 'policy.created')
    OR
    (event_type = 'policy.validation_failed'
      AND reason_code IN (
        'policy.expression_invalid',
        'policy.action_invalid',
        'policy.route_target_invalid',
        'policy.context_invalid',
        'policy.evaluation_timeout'
      ))
  ),
  CHECK (
    event_type != 'policy.validation_failed'
    OR json_type(payload_json, '$.diagnostic_codes') IS NULL
    OR (
      json_type(payload_json, '$.diagnostic_codes') = 'array'
      AND json_array_length(payload_json, '$.diagnostic_codes') <= 8
    )
  )
);

-- 仅排除 Dry-Run 事件，其余 Policy 历史按 tenant_id/request_id 原样恢复到 0034 结构。
INSERT INTO policy_events_prd046_old(
  policy_event_id, tenant_id, policy_id, event_type, actor_user_id,
  request_id, trace_id, reason_code, payload_json, created_at
)
SELECT
  policy_event_id, tenant_id, policy_id, event_type, actor_user_id,
  request_id, trace_id, reason_code, payload_json, created_at
FROM policy_events
WHERE event_type != 'policy.dry_run_executed';

-- 清理可能残留的临时守卫，确保本轮回滚计数判断只基于当前事务事实。
DROP TABLE IF EXISTS policy_events_prd046_down_guard;
CREATE TABLE policy_events_prd046_down_guard (
  expected_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (expected_count = copied_count)
);
-- 写入过滤后源计数与目标计数，任何不一致均失败关闭并保留原始表。
INSERT INTO policy_events_prd046_down_guard(expected_count, copied_count)
SELECT
  (SELECT COUNT(*) FROM policy_events WHERE event_type != 'policy.dry_run_executed'),
  (SELECT COUNT(*) FROM policy_events_prd046_old);
DROP TABLE policy_events_prd046_down_guard;

DROP TABLE policy_events;
-- 事件数量验证通过后再切换表名，避免回滚期间暴露缺行或跨租户错配结构。
ALTER TABLE policy_events_prd046_old RENAME TO policy_events;

CREATE UNIQUE INDEX idx_policy_events_created_unique
  ON policy_events(tenant_id, policy_id)
  WHERE event_type = 'policy.created';
CREATE UNIQUE INDEX idx_policy_events_validation_request_unique
  ON policy_events(tenant_id, actor_user_id, request_id)
  WHERE event_type = 'policy.validation_failed';
CREATE INDEX idx_policy_events_tenant_time
  ON policy_events(tenant_id, created_at DESC, policy_event_id DESC);
CREATE INDEX idx_policy_events_tenant_request
  ON policy_events(tenant_id, request_id);

CREATE TRIGGER trg_policy_events_owner_insert
BEFORE INSERT ON policy_events
FOR EACH ROW
WHEN
  NOT EXISTS (
    SELECT 1 FROM users u
    WHERE u.tenant_id = NEW.tenant_id AND u.user_id = NEW.actor_user_id
  )
  OR (
    NEW.policy_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM policies p
      WHERE p.tenant_id = NEW.tenant_id AND p.policy_id = NEW.policy_id
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'policy_event_owner_mismatch');
END;
CREATE TRIGGER trg_policy_events_immutable_update
BEFORE UPDATE ON policy_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_event_immutable');
END;
CREATE TRIGGER trg_policy_events_immutable_delete
BEFORE DELETE ON policy_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_event_immutable');
END;

-- Dry-Run 的事件与审计已通过过滤重建解除引用，最后移除 explanation 不可变对象。
DROP TRIGGER IF EXISTS trg_policy_explanations_immutable_delete;
DROP TRIGGER IF EXISTS trg_policy_explanations_immutable_update;
-- 先移除 actor 查询索引，避免删除 explanation 表时遗留 PRD-046 专属结构。
DROP INDEX IF EXISTS idx_policy_explanations_tenant_actor_time;
-- 再移除租户时间索引；每项显式回滚便于验证 0034 schema 精确恢复。
DROP INDEX IF EXISTS idx_policy_explanations_tenant_time;
-- 关联事件和审计已过滤且计数校验完成，此时才可安全删除 explanation 主表。
DROP TABLE IF EXISTS policy_explanations;

-- 回滚同样在提交前验证完整性；down 后必须可直接由 0034 应用继续运行。
DROP TABLE IF EXISTS policy_dry_run_down_integrity_guard;
CREATE TABLE policy_dry_run_down_integrity_guard (
  foreign_key_issue_count INTEGER NOT NULL CHECK (foreign_key_issue_count = 0),
  integrity_issue_count INTEGER NOT NULL CHECK (integrity_issue_count = 0)
);
-- down 迁移同样把 FK/integrity 结果写入零值守卫，异常时不得提交伪装成功。
INSERT INTO policy_dry_run_down_integrity_guard(foreign_key_issue_count, integrity_issue_count)
SELECT
  (SELECT COUNT(*) FROM pragma_foreign_key_check),
  (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check != 'ok');
DROP TABLE policy_dry_run_down_integrity_guard;

COMMIT;
PRAGMA foreign_keys = ON;
