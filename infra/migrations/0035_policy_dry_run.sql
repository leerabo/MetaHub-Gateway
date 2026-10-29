-- 迁移职责：为 PRD-046 增加策略单请求预演的不可变安全摘要，并扩展 Policy 事件与审计白名单。
-- 隐私边界：数据库只保存确定性决策摘要；Prompt、Prompt hash、DSL/AST、预算余额、Provider URL、
-- Key、Token 与比较实际值均不得进入 explanation、event 或 audit。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- PolicyExplanation 是一次已完成预演的稳定安全事实。完整决策树只存在于当前响应内存中。
CREATE TABLE policy_explanations (
  policy_explanation_id TEXT PRIMARY KEY CHECK (
    length(policy_explanation_id) = 37
    AND substr(policy_explanation_id, 1, 5) = 'pexp_'
    AND substr(policy_explanation_id, 6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
  status TEXT NOT NULL CHECK (status = 'completed'),
  version INTEGER NOT NULL CHECK (typeof(version) = 'integer' AND version = 1),
  policy_source TEXT NOT NULL CHECK (policy_source = 'active_candidate'),
  policy_set_fingerprint TEXT NOT NULL CHECK (
    length(policy_set_fingerprint) = 64
    AND policy_set_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  published_snapshot_version INTEGER CHECK (
    published_snapshot_version IS NULL
    OR (typeof(published_snapshot_version) = 'integer' AND published_snapshot_version >= 1)
  ),
  decision TEXT NOT NULL CHECK (
    decision IN ('allow', 'deny', 'mask', 'log_only', 'route_to')
  ),
  reason_code TEXT NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 128),
  matched_policy_id TEXT CHECK (
    matched_policy_id IS NULL OR length(matched_policy_id) BETWEEN 5 AND 128
  ),
  matched_priority INTEGER CHECK (
    matched_priority IS NULL
    OR (typeof(matched_priority) = 'integer' AND matched_priority BETWEEN 1 AND 10000)
  ),
  matched_action TEXT CHECK (
    matched_action IS NULL
    OR matched_action IN ('allow', 'deny', 'mask', 'log_only', 'route_to')
  ),
  route_status TEXT NOT NULL CHECK (
    route_status IN ('selected', 'unavailable', 'not_evaluated')
  ),
  dlp_status TEXT NOT NULL CHECK (
    dlp_status IN ('clean', 'matched', 'blocked', 'not_evaluated')
  ),
  budget_status TEXT NOT NULL CHECK (
    budget_status IN (
      'no_rules', 'within_limit', 'threshold_reached', 'exhausted', 'not_evaluated'
    )
  ),
  would_error_code TEXT CHECK (
    would_error_code IS NULL OR length(would_error_code) BETWEEN 1 AND 128
  ),
  checked_policy_count INTEGER NOT NULL CHECK (
    typeof(checked_policy_count) = 'integer' AND checked_policy_count BETWEEN 0 AND 200
  ),
  runtime_compatible INTEGER NOT NULL CHECK (
    typeof(runtime_compatible) = 'integer' AND runtime_compatible IN (0, 1)
  ),
  coverage_complete INTEGER NOT NULL CHECK (
    typeof(coverage_complete) = 'integer'
    AND coverage_complete IN (0, 1)
  ),
  explanation_truncated INTEGER NOT NULL CHECK (
    typeof(explanation_truncated) = 'integer' AND explanation_truncated IN (0, 1)
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  UNIQUE (tenant_id, policy_explanation_id),
  UNIQUE (tenant_id, actor_user_id, request_id),
  CHECK (updated_at = created_at)
);

-- 两个查询索引都先按 tenant 收敛，actor 维度只用于管理员追溯本人的 exactly-once 事实。
CREATE INDEX idx_policy_explanations_tenant_time
  ON policy_explanations(tenant_id, created_at DESC);
CREATE INDEX idx_policy_explanations_tenant_actor_time
  ON policy_explanations(tenant_id, actor_user_id, created_at DESC);

-- Explanation 是追加式安全事实；普通业务连接不得改写或删除历史摘要。
CREATE TRIGGER trg_policy_explanations_immutable_update
BEFORE UPDATE ON policy_explanations
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_explanation_immutable');
END;
CREATE TRIGGER trg_policy_explanations_immutable_delete
BEFORE DELETE ON policy_explanations
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_explanation_immutable');
END;

-- policy_events 的 event_type 与 event/reason 语义同时扩展；0033 历史字段必须逐列保真。
DROP TABLE IF EXISTS policy_events_prd046_new;
CREATE TABLE policy_events_prd046_new (
  policy_event_id TEXT PRIMARY KEY CHECK (
    length(policy_event_id) BETWEEN 6 AND 128 AND policy_event_id GLOB 'pevt_*'
  ),
  tenant_id TEXT NOT NULL,
  policy_id TEXT,
  event_type TEXT NOT NULL CHECK (
    event_type IN ('policy.created', 'policy.validation_failed', 'policy.dry_run_executed')
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
    COALESCE(
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
      OR
      (event_type = 'policy.dry_run_executed'
        AND reason_code = 'policy.dry_run_executed'),
      0
    )
  ),
  CHECK (
    event_type != 'policy.validation_failed'
    OR json_type(payload_json, '$.diagnostic_codes') IS NULL
    OR (
      json_type(payload_json, '$.diagnostic_codes') = 'array'
      AND json_array_length(payload_json, '$.diagnostic_codes') <= 8
    )
  ),
  -- Dry-Run 事件只允许 PRD 指定的 13 个摘要键；移除全部白名单键后必须恰为空对象。
  CHECK (
    event_type != 'policy.dry_run_executed'
    OR COALESCE(
      json_remove(
        payload_json,
        '$.policy_explanation_id', '$.request_id', '$.policy_set_fingerprint',
        '$.decision', '$.decision_reason_code', '$.matched_policy_id',
        '$.route_status', '$.dlp_status', '$.budget_status', '$.would_error_code',
        '$.checked_policy_count', '$.runtime_compatible', '$.coverage_complete'
      ) = '{}'
      AND json_type(payload_json, '$.policy_explanation_id') = 'text'
      AND length(json_extract(payload_json, '$.policy_explanation_id')) = 37
      AND substr(json_extract(payload_json, '$.policy_explanation_id'), 1, 5) = 'pexp_'
      AND substr(json_extract(payload_json, '$.policy_explanation_id'), 6)
        NOT GLOB '*[^0-9a-f]*'
      AND json_type(payload_json, '$.request_id') = 'text'
      AND json_extract(payload_json, '$.request_id') = request_id
      AND json_type(payload_json, '$.policy_set_fingerprint') = 'text'
      AND length(json_extract(payload_json, '$.policy_set_fingerprint')) = 64
      AND json_extract(payload_json, '$.policy_set_fingerprint') NOT GLOB '*[^0-9a-f]*'
      AND json_type(payload_json, '$.decision') = 'text'
      AND json_extract(payload_json, '$.decision')
        IN ('allow', 'deny', 'mask', 'log_only', 'route_to')
      AND json_type(payload_json, '$.decision_reason_code') = 'text'
      AND length(json_extract(payload_json, '$.decision_reason_code')) BETWEEN 1 AND 128
      AND (
        (policy_id IS NULL AND json_type(payload_json, '$.matched_policy_id') = 'null')
        OR (
          policy_id IS NOT NULL
          AND json_type(payload_json, '$.matched_policy_id') = 'text'
          AND json_extract(payload_json, '$.matched_policy_id') = policy_id
        )
      )
      AND json_extract(payload_json, '$.route_status')
        IN ('selected', 'unavailable', 'not_evaluated')
      AND json_extract(payload_json, '$.dlp_status')
        IN ('clean', 'matched', 'blocked', 'not_evaluated')
      AND json_extract(payload_json, '$.budget_status')
        IN ('no_rules', 'within_limit', 'threshold_reached', 'exhausted', 'not_evaluated')
      AND (
        json_type(payload_json, '$.would_error_code') = 'null'
        OR (
          json_type(payload_json, '$.would_error_code') = 'text'
          AND length(json_extract(payload_json, '$.would_error_code')) BETWEEN 1 AND 128
          AND json_extract(payload_json, '$.would_error_code') GLOB 'gateway.*'
        )
      )
      AND json_type(payload_json, '$.checked_policy_count') = 'integer'
      AND json_extract(payload_json, '$.checked_policy_count') BETWEEN 0 AND 200
      AND json_type(payload_json, '$.runtime_compatible') IN ('true', 'false')
      AND json_type(payload_json, '$.coverage_complete') = 'false',
      0
    )
  )
);

-- 全列复制既有 Policy 事件并保留 tenant_id/request_id，避免新增约束时改写历史租户事实。
INSERT INTO policy_events_prd046_new(
  policy_event_id, tenant_id, policy_id, event_type, actor_user_id,
  request_id, trace_id, reason_code, payload_json, created_at
)
SELECT
  policy_event_id, tenant_id, policy_id, event_type, actor_user_id,
  request_id, trace_id, reason_code, payload_json, created_at
FROM policy_events;

-- 显式计数守卫使任何复制数量差异在 drop 旧表之前中止并回滚整个迁移。
DROP TABLE IF EXISTS policy_events_prd046_copy_guard;
CREATE TABLE policy_events_prd046_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (source_count = copied_count)
);
-- 在删除旧表前写入源/目标计数；任何差异触发 CHECK 并回滚整个事务。
INSERT INTO policy_events_prd046_copy_guard(source_count, copied_count)
SELECT (SELECT COUNT(*) FROM policy_events), (SELECT COUNT(*) FROM policy_events_prd046_new);
DROP TABLE policy_events_prd046_copy_guard;

DROP TABLE policy_events;
-- 只有复制守卫通过后才切换表名，确保应用永远看不到半迁移的事件结构。
ALTER TABLE policy_events_prd046_new RENAME TO policy_events;

-- 完整恢复 0033 的部分唯一索引、查询索引与 owner/immutable 触发器，再增加 Dry-Run 键。
CREATE UNIQUE INDEX idx_policy_events_created_unique
  ON policy_events(tenant_id, policy_id)
  WHERE event_type = 'policy.created';
CREATE UNIQUE INDEX idx_policy_events_validation_request_unique
  ON policy_events(tenant_id, actor_user_id, request_id)
  WHERE event_type = 'policy.validation_failed';
CREATE UNIQUE INDEX idx_policy_events_dry_run_request_unique
  ON policy_events(tenant_id, actor_user_id, request_id)
  WHERE event_type = 'policy.dry_run_executed';
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

-- audit_logs 仅扩展动作/对象枚举；0034 的 17 列和全部历史事实逐字段复制。
DROP TABLE IF EXISTS audit_logs_prd046_new;
CREATE TABLE audit_logs_prd046_new (
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
      'policy_snapshot.published', 'policy_snapshot.rolled_back',
      'policy.dry_run_executed'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot', 'policy_explanation'
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

-- 审计日志按原列原值复制，保留 tenant_id、actor 与链路字段，不重算不可变历史摘要。
INSERT INTO audit_logs_prd046_new(
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

-- 创建独立审计复制守卫，防止上次失败迁移留下的临时表影响本次原子校验。
DROP TABLE IF EXISTS audit_logs_prd046_copy_guard;
CREATE TABLE audit_logs_prd046_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (source_count = copied_count)
);
-- 审计源表和新表计数必须完全一致，否则在丢弃旧表前失败关闭。
INSERT INTO audit_logs_prd046_copy_guard(source_count, copied_count)
SELECT (SELECT COUNT(*) FROM audit_logs), (SELECT COUNT(*) FROM audit_logs_prd046_new);
DROP TABLE audit_logs_prd046_copy_guard;

DROP TABLE audit_logs;
-- 计数一致后再切换审计表，后续索引和不可变触发器均绑定到完整数据集。
ALTER TABLE audit_logs_prd046_new RENAME TO audit_logs;

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
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action = 'policy.dry_run_executed';

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

-- 在提交前把 FK 与 integrity PRAGMA 结果转换为 CHECK 守卫；任何异常都回滚全部 DDL/复制。
DROP TABLE IF EXISTS policy_dry_run_integrity_guard;
CREATE TABLE policy_dry_run_integrity_guard (
  foreign_key_issue_count INTEGER NOT NULL CHECK (foreign_key_issue_count = 0),
  integrity_issue_count INTEGER NOT NULL CHECK (integrity_issue_count = 0)
);
-- 把 FK 与 integrity 检查结果写入零值守卫，异常时阻止提交而不是留下可运行的坏库。
INSERT INTO policy_dry_run_integrity_guard(foreign_key_issue_count, integrity_issue_count)
SELECT
  (SELECT COUNT(*) FROM pragma_foreign_key_check),
  (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check != 'ok');
DROP TABLE policy_dry_run_integrity_guard;

COMMIT;
PRAGMA foreign_keys = ON;
