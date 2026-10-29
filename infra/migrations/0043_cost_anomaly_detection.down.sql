-- PRD-053 条件回滚：只允许在没有成本异常业务事实和外部事件合同时恢复 0042。
-- 四张新表、成本异常 Audit 和 Webhook 事实都不可静默丢失，任一存在必须原子拒绝。
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- 结构门禁位于所有持久 DDL 前；既验证完整 0043，也拒绝未知索引、触发器、View 和外键依赖。
CREATE TEMP TABLE prd053_down_prerequisite_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd053_down_prerequisite_guard
BEFORE INSERT ON prd053_down_prerequisite_guard
FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd053_down_prerequisite_missing');
END;
-- 汇总版本链、0043 对象和未知依赖，结构漂移时禁止执行任何持久回滚动作。
INSERT INTO prd053_down_prerequisite_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM schema_migrations
    WHERE version='0043' AND status='applied'
  )=1 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM schema_migrations
    WHERE version='0042' AND status='applied'
  )=1 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='table' AND name IN (
      'cost_anomaly_detection_runs', 'cost_anomalies',
      'cost_anomaly_actions', 'temporary_rate_limit_overrides',
      'audit_logs', 'webhook_endpoints', 'webhook_deliveries',
      'webhook_delivery_attempts'
    )
  )=8 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND sql IS NOT NULL AND name IN (
      'idx_cost_anomaly_runs_status_window_prd053',
      'idx_cost_anomaly_runs_tenant_window_prd053',
      'idx_cost_anomalies_tenant_status_sequence_prd053',
      'idx_cost_anomalies_rule_time_prd053',
      'idx_cost_anomalies_scope_time_prd053',
      'idx_cost_anomalies_pending_event_prd053',
      'idx_cost_anomaly_actions_tenant_anomaly_prd053',
      'idx_temporary_rate_limit_overrides_key_prd053'
    )
  )=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='index' AND sql IS NOT NULL
       AND tbl_name IN (
         'cost_anomaly_detection_runs', 'cost_anomalies',
         'cost_anomaly_actions', 'temporary_rate_limit_overrides'
       )
       AND name NOT IN (
         'idx_cost_anomaly_runs_status_window_prd053',
         'idx_cost_anomaly_runs_tenant_window_prd053',
         'idx_cost_anomalies_tenant_status_sequence_prd053',
         'idx_cost_anomalies_rule_time_prd053',
         'idx_cost_anomalies_scope_time_prd053',
         'idx_cost_anomalies_pending_event_prd053',
         'idx_cost_anomaly_actions_tenant_anomaly_prd053',
         'idx_temporary_rate_limit_overrides_key_prd053'
       ))
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND name IN (
      'trg_cost_anomaly_runs_owner_insert_prd053',
      'trg_cost_anomaly_runs_conflict_insert_prd053',
      'trg_cost_anomaly_runs_rowid_insert_prd053',
      'trg_cost_anomaly_runs_state_update_prd053',
      'trg_cost_anomaly_runs_immutable_delete_prd053',
      'trg_cost_anomalies_owner_insert_prd053',
      'trg_cost_anomalies_conflict_insert_prd053',
      'trg_cost_anomalies_state_update_prd053',
      'trg_cost_anomalies_immutable_delete_prd053',
      'trg_cost_anomaly_actions_owner_insert_prd053',
      'trg_cost_anomaly_actions_conflict_insert_prd053',
      'trg_cost_anomaly_actions_rowid_insert_prd053',
      'trg_cost_anomaly_actions_immutable_update_prd053',
      'trg_cost_anomaly_actions_immutable_delete_prd053',
      'trg_temporary_rate_limit_overrides_owner_insert_prd053',
      'trg_temporary_rate_limit_overrides_conflict_insert_prd053',
      'trg_temporary_rate_limit_overrides_rowid_insert_prd053',
      'trg_temporary_rate_limit_overrides_release_update_prd053',
      'trg_temporary_rate_limit_overrides_immutable_delete_prd053'
    )
  )=19 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND tbl_name IN (
      'cost_anomaly_detection_runs', 'cost_anomalies',
      'cost_anomaly_actions', 'temporary_rate_limit_overrides'
    )
  )=19 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND tbl_name='audit_logs' AND sql IS NOT NULL
      AND name IN (
        'idx_audit_logs_tenant_time', 'idx_audit_logs_tenant_actor_time',
        'idx_audit_logs_tenant_object_time', 'idx_audit_logs_tenant_action_time',
        'idx_audit_logs_tenant_request', 'idx_audit_logs_dry_run_request_unique',
        'idx_audit_logs_model_quality_request_unique',
        'idx_audit_logs_finops_adjustment_unique'
      )
  )=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='index' AND tbl_name='audit_logs' AND sql IS NOT NULL
       AND name NOT IN (
         'idx_audit_logs_tenant_time', 'idx_audit_logs_tenant_actor_time',
         'idx_audit_logs_tenant_object_time', 'idx_audit_logs_tenant_action_time',
         'idx_audit_logs_tenant_request', 'idx_audit_logs_dry_run_request_unique',
         'idx_audit_logs_model_quality_request_unique',
         'idx_audit_logs_finops_adjustment_unique'
       ))
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND tbl_name='audit_logs' AND name IN (
      'trg_audit_logs_owner_insert', 'trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete', 'trg_audit_logs_prd050_conflict_insert',
      'trg_audit_logs_prd050_rowid_insert'
    )
  )=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='trigger' AND tbl_name='audit_logs' AND name NOT IN (
       'trg_audit_logs_owner_insert', 'trg_audit_logs_immutable_update',
       'trg_audit_logs_immutable_delete', 'trg_audit_logs_prd050_conflict_insert',
       'trg_audit_logs_prd050_rowid_insert'
     ))
  + CASE WHEN (
    SELECT sql LIKE '%cost_anomaly.temporary_rate_limit_released%'
      AND sql LIKE '%cost_anomaly%' FROM sqlite_master
    WHERE type='table' AND name='audit_logs'
  ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='view' AND (
       lower(COALESCE(sql, '')) LIKE '%cost_anomaly_detection_runs%'
       OR lower(COALESCE(sql, '')) LIKE '%cost_anomalies%'
       OR lower(COALESCE(sql, '')) LIKE '%cost_anomaly_actions%'
       OR lower(COALESCE(sql, '')) LIKE '%temporary_rate_limit_overrides%'
     ))
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='trigger'
       AND tbl_name NOT IN (
         'audit_logs', 'cost_anomaly_detection_runs', 'cost_anomalies',
         'cost_anomaly_actions', 'temporary_rate_limit_overrides'
       )
       AND (
         lower(COALESCE(sql, '')) LIKE '%cost_anomaly_detection_runs%'
         OR lower(COALESCE(sql, '')) LIKE '%cost_anomalies%'
         OR lower(COALESCE(sql, '')) LIKE '%cost_anomaly_actions%'
         OR lower(COALESCE(sql, '')) LIKE '%temporary_rate_limit_overrides%'
       ))
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='table'
       AND name NOT IN (
         'cost_anomaly_detection_runs', 'cost_anomalies',
         'cost_anomaly_actions', 'temporary_rate_limit_overrides'
       )
       AND (
         lower(COALESCE(sql, '')) LIKE '%references%cost_anomaly_detection_runs%'
         OR lower(COALESCE(sql, '')) LIKE '%references%cost_anomalies%'
         OR lower(COALESCE(sql, '')) LIKE '%references%cost_anomaly_actions%'
         OR lower(COALESCE(sql, '')) LIKE '%references%temporary_rate_limit_overrides%'
       ))
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE name='audit_logs_prd053_down');
DROP TRIGGER trg_prd053_down_prerequisite_guard;
DROP TABLE prd053_down_prerequisite_guard;

-- 不可逆事实门禁覆盖数据库事实、外部订阅、投递尝试和 Audit；必须先于任何持久 DROP。
CREATE TEMP TABLE prd053_down_business_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd053_down_business_guard
BEFORE INSERT ON prd053_down_business_guard
FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd053_down_business_facts_present');
END;
-- 同时核对内部事实、成本 Audit 和 Webhook 合同，防止回滚静默丢失可追踪证据。
INSERT INTO prd053_down_business_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM cost_anomaly_detection_runs)
  + (SELECT COUNT(*) FROM cost_anomalies)
  + (SELECT COUNT(*) FROM cost_anomaly_actions)
  + (SELECT COUNT(*) FROM temporary_rate_limit_overrides)
  + (SELECT COUNT(*) FROM audit_logs
     WHERE action IN (
       'cost_anomaly.acknowledged', 'cost_anomaly.dismissed',
       'cost_anomaly.temporary_rate_limit_applied',
       'cost_anomaly.temporary_rate_limit_released'
     ) OR object_type='cost_anomaly')
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
     WHERE CASE
       WHEN json_valid(endpoint.event_types_json)
        AND json_type(endpoint.event_types_json)='array'
       THEN EXISTS (
         SELECT 1 FROM json_each(endpoint.event_types_json) event_type
         WHERE event_type.type='text' AND event_type.value='cost.anomaly_detected'
       )
       ELSE 0
     END)
  + (SELECT COUNT(*) FROM webhook_deliveries delivery
     WHERE delivery.event_type='cost.anomaly_detected')
  + (SELECT COUNT(*)
     FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='cost.anomaly_detected');
DROP TRIGGER trg_prd053_down_business_guard;
DROP TABLE prd053_down_business_guard;

-- 先移除 Audit 对 action 的反查，再按外键子到父顺序删除四张空表，避免留下半套状态机。
DROP TRIGGER trg_audit_logs_prd050_rowid_insert;
DROP TRIGGER trg_audit_logs_prd050_conflict_insert;
DROP TRIGGER trg_audit_logs_immutable_delete;
DROP TRIGGER trg_audit_logs_immutable_update;
DROP TRIGGER trg_audit_logs_owner_insert;

DROP TRIGGER trg_temporary_rate_limit_overrides_immutable_delete_prd053;
DROP TRIGGER trg_temporary_rate_limit_overrides_release_update_prd053;
DROP TRIGGER trg_temporary_rate_limit_overrides_rowid_insert_prd053;
DROP TRIGGER trg_temporary_rate_limit_overrides_conflict_insert_prd053;
DROP TRIGGER trg_temporary_rate_limit_overrides_owner_insert_prd053;
DROP TRIGGER trg_cost_anomaly_actions_immutable_delete_prd053;
DROP TRIGGER trg_cost_anomaly_actions_immutable_update_prd053;
DROP TRIGGER trg_cost_anomaly_actions_rowid_insert_prd053;
DROP TRIGGER trg_cost_anomaly_actions_conflict_insert_prd053;
DROP TRIGGER trg_cost_anomaly_actions_owner_insert_prd053;
DROP TRIGGER trg_cost_anomalies_immutable_delete_prd053;
DROP TRIGGER trg_cost_anomalies_state_update_prd053;
DROP TRIGGER trg_cost_anomalies_conflict_insert_prd053;
DROP TRIGGER trg_cost_anomalies_owner_insert_prd053;
DROP TRIGGER trg_cost_anomaly_runs_immutable_delete_prd053;
DROP TRIGGER trg_cost_anomaly_runs_state_update_prd053;
DROP TRIGGER trg_cost_anomaly_runs_rowid_insert_prd053;
DROP TRIGGER trg_cost_anomaly_runs_conflict_insert_prd053;
DROP TRIGGER trg_cost_anomaly_runs_owner_insert_prd053;

DROP INDEX idx_temporary_rate_limit_overrides_key_prd053;
DROP INDEX idx_cost_anomaly_actions_tenant_anomaly_prd053;
DROP INDEX idx_cost_anomalies_pending_event_prd053;
DROP INDEX idx_cost_anomalies_scope_time_prd053;
DROP INDEX idx_cost_anomalies_rule_time_prd053;
DROP INDEX idx_cost_anomalies_tenant_status_sequence_prd053;
DROP INDEX idx_cost_anomaly_runs_tenant_window_prd053;
DROP INDEX idx_cost_anomaly_runs_status_window_prd053;
DROP TABLE temporary_rate_limit_overrides;
DROP TABLE cost_anomaly_actions;
DROP TABLE cost_anomalies;
DROP TABLE cost_anomaly_detection_runs;

-- Audit 恢复到 0042 的 FinOps 枚举；17 列、rowid、NULL 和 JSON 正文逐值保真。
CREATE TABLE audit_logs_prd053_down (
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
      'policy.dry_run_executed',
      'routing_policy.created', 'routing_policy.updated',
      'model_quality_score.updated',
      'cache_affinity.created', 'cache_affinity.updated',
      'finops.adjustment_created'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot', 'policy_explanation', 'routing_policy',
      'model_quality_score', 'cache_affinity', 'finops_adjustment'
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
-- 显式复制 rowid 与全部可空字段，把 0043 Audit 保真恢复到 0042 枚举。
INSERT INTO audit_logs_prd053_down(
  rowid, audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
)
SELECT
  rowid, audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
FROM audit_logs;

CREATE TEMP TABLE prd053_down_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd053_down_audit_copy_guard
BEFORE INSERT ON prd053_down_audit_copy_guard
FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0
  OR NEW.row_difference_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd053_down_audit_copy_failed');
END;
-- 双向比较源表与恢复表，任何计数、NULL 或字段差异都必须原子终止 down。
INSERT INTO prd053_down_audit_copy_guard(
  source_count, copied_count, null_mismatch_count, row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd053_down),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd053_down WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd053_down WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd053_down WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid, * FROM audit_logs
    EXCEPT SELECT rowid, * FROM audit_logs_prd053_down
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid, * FROM audit_logs_prd053_down
    EXCEPT SELECT rowid, * FROM audit_logs
  ));
DROP TRIGGER trg_prd053_down_audit_copy_guard;
DROP TABLE prd053_down_audit_copy_guard;
DROP TABLE audit_logs;
-- 只有保真复制通过后才切回正式 Audit 名称，保证回滚期间结构不可半完成。
ALTER TABLE audit_logs_prd053_down RENAME TO audit_logs;

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
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id, object_id)
  WHERE action='finops.adjustment_created';

-- 恢复 0042 的 Audit owner 与 append-only 语义，继续保护用户归属及 FinOps 调整关系。
CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users user
  WHERE user.user_id=NEW.actor_user_id AND user.tenant_id=NEW.tenant_id
)
OR (
  NEW.action='finops.adjustment_created'
  AND (
    NEW.object_type!='finops_adjustment'
    OR NOT EXISTS (
      SELECT 1 FROM finops_adjustments adjustment
      WHERE adjustment.finops_adjustment_id=NEW.object_id
        AND adjustment.tenant_id=NEW.tenant_id
        AND adjustment.actor_user_id=NEW.actor_user_id
    )
  )
)
OR (NEW.object_type='finops_adjustment' AND NEW.action!='finops.adjustment_created')
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
CREATE TRIGGER trg_audit_logs_prd050_conflict_insert
BEFORE INSERT ON audit_logs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM audit_logs current
  WHERE current.audit_log_id=NEW.audit_log_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
    OR (
      NEW.action IN ('policy.dry_run_executed', 'model_quality_score.updated')
      AND current.action=NEW.action
      AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id
      AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action='finops.adjustment_created'
      AND current.action='finops.adjustment_created'
      AND current.tenant_id=NEW.tenant_id
      AND current.object_id=NEW.object_id
    )
)
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs
FOR EACH ROW
WHEN NEW.rowid<0
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;

-- 提交前验证 0043 对象已清除、0042 Audit 已恢复且数据库结构仍完整；失败会回滚整个 down。
CREATE TEMP TABLE prd053_down_integrity_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd053_down_integrity_guard
BEFORE INSERT ON prd053_down_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd053_down_integrity_failed');
END;
-- 汇总 0043 对象清理、0042 Audit 恢复和 SQLite 完整性，异常时撤销整个 down。
INSERT INTO prd053_down_integrity_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM sqlite_master WHERE name IN (
    'cost_anomaly_detection_runs', 'cost_anomalies', 'cost_anomaly_actions',
    'temporary_rate_limit_overrides',
    'idx_cost_anomaly_runs_status_window_prd053',
    'idx_cost_anomaly_runs_tenant_window_prd053',
    'idx_cost_anomalies_tenant_status_sequence_prd053',
    'idx_cost_anomalies_rule_time_prd053',
    'idx_cost_anomalies_scope_time_prd053',
    'idx_cost_anomalies_pending_event_prd053',
    'idx_cost_anomaly_actions_tenant_anomaly_prd053',
    'idx_temporary_rate_limit_overrides_key_prd053'
  ))
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND tbl_name='audit_logs' AND sql IS NOT NULL
  )=8 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND tbl_name='audit_logs'
  )=5 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT sql LIKE '%finops.adjustment_created%'
      AND sql NOT LIKE '%cost_anomaly.%'
      AND sql NOT LIKE '%cost_anomaly''%'
    FROM sqlite_master WHERE type='table' AND name='audit_logs'
  ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + CASE WHEN (SELECT integrity_check FROM pragma_integrity_check)='ok'
    THEN 0 ELSE 1 END;
DROP TRIGGER trg_prd053_down_integrity_guard;
DROP TABLE prd053_down_integrity_guard;

COMMIT;
