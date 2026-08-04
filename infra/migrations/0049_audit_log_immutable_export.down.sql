-- 回滚职责：仅在没有任何 PRD-058 业务事实、订阅、投递或未知依赖时恢复 0048 schema。
-- 不可删除审计是业务约束；down 不能通过静默删除 AuditExport、Webhook 或审计证据完成降级。
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd058_down_guard (
  business_fact_count INTEGER NOT NULL,
  unknown_dependency_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd058_down_guard
BEFORE INSERT ON prd058_down_guard FOR EACH ROW
WHEN NEW.business_fact_count!=0 OR NEW.unknown_dependency_count!=0
BEGIN SELECT RAISE(ABORT,'prd058_down_blocked'); END;

-- 在任何持久 DDL 前统计导出、source outbox、审计、订阅、投递、attempt 与死信事实。
INSERT INTO prd058_down_guard(business_fact_count,unknown_dependency_count)
SELECT
  (SELECT COUNT(*) FROM audit_exports)
  + (SELECT COUNT(*) FROM audit_export_events)
  + (SELECT COUNT(*) FROM audit_logs
      WHERE action='audit.exported' OR object_type='audit_export')
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
      WHERE (json_valid(endpoint.event_types_json)
          AND EXISTS (
            SELECT 1 FROM json_each(endpoint.event_types_json) event_type
            WHERE event_type.value='audit.exported'
          ))
        OR lower(endpoint.event_types_json) LIKE '%audit.exported%')
  + (SELECT COUNT(*) FROM webhook_deliveries
      WHERE event_type='audit.exported')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
      JOIN webhook_deliveries delivery
        ON delivery.tenant_id=attempt.tenant_id
       AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
      WHERE delivery.event_type='audit.exported')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
      WHERE event_type='audit.exported'),
  -- 未知 view/trigger/FK/index 可能在降级时失效，必须由其所属迁移先行回退。
  (SELECT COUNT(*) FROM sqlite_schema object
    WHERE object.sql IS NOT NULL
      AND (
        lower(object.sql) LIKE '%audit_exports%'
        OR lower(object.sql) LIKE '%audit_export_events%'
        OR lower(object.sql) LIKE '%audit_log_export_sequences%'
        OR lower(object.sql) LIKE '%audit.exported%'
        OR lower(object.sql) LIKE '%audit_export%'
      )
      AND object.name NOT IN (
        'audit_logs','audit_log_export_sequences','audit_exports','audit_export_events',
        'idx_audit_log_export_sequences_tenant_sequence_prd058',
        'idx_audit_exports_tenant_time','idx_audit_exports_tenant_actor_time',
        'idx_audit_export_events_due_prd058','idx_audit_export_events_tenant_status_prd058',
        'trg_audit_log_export_sequences_owner_insert_prd058',
        'trg_audit_log_export_sequences_conflict_insert_prd058',
        'trg_audit_log_export_sequences_rowid_insert_prd058',
        'trg_audit_log_export_sequences_update_prd058',
        'trg_audit_log_export_sequences_delete_prd058',
        'trg_audit_exports_owner_insert_prd058','trg_audit_exports_conflict_insert_prd058',
        'trg_audit_exports_rowid_insert_prd058','trg_audit_exports_update_prd058',
        'trg_audit_exports_delete_prd058','trg_audit_export_events_owner_insert_prd058',
        'trg_audit_export_events_state_update_prd058',
        'trg_audit_export_events_conflict_insert_prd058',
        'trg_audit_export_events_rowid_insert_prd058','trg_audit_export_events_delete_prd058',
        'trg_audit_logs_owner_insert','trg_audit_logs_export_sequence_insert_prd058'
      ))
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.type='index' AND object.tbl_name='audit_logs' AND object.sql IS NOT NULL
        AND object.name NOT IN (
          'idx_audit_logs_tenant_time','idx_audit_logs_tenant_actor_time',
          'idx_audit_logs_tenant_object_time','idx_audit_logs_tenant_action_time',
          'idx_audit_logs_tenant_request','idx_audit_logs_dry_run_request_unique',
          'idx_audit_logs_model_quality_request_unique',
          'idx_audit_logs_finops_adjustment_unique'
        ))
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.type='trigger' AND object.tbl_name='audit_logs'
        AND object.name NOT IN (
          'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
          'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
          'trg_audit_logs_prd050_rowid_insert','trg_audit_logs_export_sequence_insert_prd058'
        ))
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.type='view' AND lower(COALESCE(object.sql,'')) LIKE '%audit_logs%')
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.type='trigger' AND object.tbl_name!='audit_logs'
        AND lower(COALESCE(object.sql,'')) LIKE '%audit_logs%'
        AND object.name NOT IN (
          'trg_audit_exports_owner_insert_prd058',
          'trg_audit_log_export_sequences_owner_insert_prd058'
        ))
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.type='table' AND object.name NOT IN (
          'audit_logs','audit_exports','audit_log_export_sequences'
        )
        AND lower(COALESCE(object.sql,'')) LIKE '%references%audit_logs%');

DROP TRIGGER trg_prd058_down_guard;
DROP TABLE prd058_down_guard;

-- 先解除 audit_logs 到 0049 表的 trigger 依赖，再按子表到父表顺序移除空结构。
DROP TRIGGER trg_audit_logs_export_sequence_insert_prd058;
DROP TRIGGER trg_audit_logs_owner_insert;

DROP TRIGGER trg_audit_export_events_owner_insert_prd058;
DROP TRIGGER trg_audit_export_events_state_update_prd058;
DROP TRIGGER trg_audit_export_events_conflict_insert_prd058;
DROP TRIGGER trg_audit_export_events_rowid_insert_prd058;
DROP TRIGGER trg_audit_export_events_delete_prd058;
DROP TABLE audit_export_events;

DROP TRIGGER trg_audit_exports_owner_insert_prd058;
DROP TRIGGER trg_audit_exports_conflict_insert_prd058;
DROP TRIGGER trg_audit_exports_rowid_insert_prd058;
DROP TRIGGER trg_audit_exports_update_prd058;
DROP TRIGGER trg_audit_exports_delete_prd058;
DROP TABLE audit_exports;

DROP TRIGGER trg_audit_log_export_sequences_owner_insert_prd058;
DROP TRIGGER trg_audit_log_export_sequences_conflict_insert_prd058;
DROP TRIGGER trg_audit_log_export_sequences_rowid_insert_prd058;
DROP TRIGGER trg_audit_log_export_sequences_update_prd058;
DROP TRIGGER trg_audit_log_export_sequences_delete_prd058;
DROP TABLE audit_log_export_sequences;

-- 恢复 0048 的 action/object CHECK；前置 guard 已证明不存在 audit.exported 正式事实。
CREATE TABLE audit_logs_prd058_down (
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
      'webhook_dead_letter.archived',
      'usage_export.requested', 'usage_export.downloaded',
      'policy.created', 'policy.updated',
      'policy_snapshot.published', 'policy_snapshot.rolled_back',
      'policy.dry_run_executed',
      'routing_policy.created', 'routing_policy.updated',
      'model_quality_score.updated',
      'cache_affinity.created', 'cache_affinity.updated',
      'finops.adjustment_created',
      'cost_anomaly.acknowledged', 'cost_anomaly.dismissed',
      'cost_anomaly.temporary_rate_limit_applied',
      'cost_anomaly.temporary_rate_limit_released',
      'error_map.created', 'error_map.updated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'webhook_dead_letter',
      'usage_export_job', 'policy', 'policy_snapshot', 'policy_explanation',
      'routing_policy', 'model_quality_score', 'cache_affinity',
      'finops_adjustment', 'cost_anomaly', 'provider_error_map'
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

-- 保真复制保留 0049 之前的全部普通审计，前置事实门禁已排除 audit.exported 行。
INSERT INTO audit_logs_prd058_down(
  rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
  action,object_type,object_id,object_name,before_summary_json,
  after_summary_json,changed_fields_json,client_ip,user_agent,
  request_id,trace_id,created_at
)
SELECT
  rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
  action,object_type,object_id,object_name,before_summary_json,
  after_summary_json,changed_fields_json,client_ip,user_agent,
  request_id,trace_id,created_at
FROM audit_logs;

CREATE TEMP TABLE prd058_down_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd058_down_audit_copy_guard
BEFORE INSERT ON prd058_down_audit_copy_guard FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0 OR NEW.row_difference_count!=0
BEGIN SELECT RAISE(ABORT,'prd058_down_audit_copy_failed'); END;
-- 删除 0049 审计表前复核行数、NULL 与 rowid 双向差集，降级不能重排历史证据。
INSERT INTO prd058_down_audit_copy_guard(
  source_count,copied_count,null_mismatch_count,row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd058_down),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd058_down WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd058_down WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd058_down WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs
    EXCEPT SELECT rowid,* FROM audit_logs_prd058_down
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs_prd058_down
    EXCEPT SELECT rowid,* FROM audit_logs
  ));
DROP TRIGGER trg_prd058_down_audit_copy_guard;
DROP TABLE prd058_down_audit_copy_guard;
DROP TABLE audit_logs;
-- 只有保真守卫成功后才恢复正式表名，避免向并发读者暴露中间结构。
ALTER TABLE audit_logs_prd058_down RENAME TO audit_logs;

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

-- 恢复 0048 审计 owner 合同，不放宽既有 FinOps、异常、error-map 与死信租户关系。
CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs FOR EACH ROW
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
OR (
  NEW.action IN (
    'cost_anomaly.acknowledged','cost_anomaly.dismissed',
    'cost_anomaly.temporary_rate_limit_applied',
    'cost_anomaly.temporary_rate_limit_released'
  )
  AND (
    NEW.object_type!='cost_anomaly'
    OR NOT EXISTS (
      SELECT 1 FROM cost_anomaly_actions action
      JOIN cost_anomalies anomaly
        ON anomaly.tenant_id=action.tenant_id
       AND anomaly.cost_anomaly_id=action.cost_anomaly_id
      WHERE action.tenant_id=NEW.tenant_id
        AND action.cost_anomaly_id=NEW.object_id
        AND action.actor_user_id=NEW.actor_user_id
        AND action.operation_request_id=NEW.request_id
        AND action.operation_trace_id=NEW.trace_id
        AND CASE action.action_type
          WHEN 'acknowledge' THEN 'cost_anomaly.acknowledged'
          WHEN 'dismiss' THEN 'cost_anomaly.dismissed'
          WHEN 'apply_temporary_rate_limit' THEN 'cost_anomaly.temporary_rate_limit_applied'
          WHEN 'release_temporary_rate_limit' THEN 'cost_anomaly.temporary_rate_limit_released'
        END=NEW.action
    )
  )
)
OR (
  NEW.object_type='cost_anomaly'
  AND NEW.action NOT IN (
    'cost_anomaly.acknowledged','cost_anomaly.dismissed',
    'cost_anomaly.temporary_rate_limit_applied',
    'cost_anomaly.temporary_rate_limit_released'
  )
)
OR (
  NEW.action IN ('error_map.created','error_map.updated')
  AND (
    NEW.object_type!='provider_error_map'
    OR NOT EXISTS (
      SELECT 1 FROM provider_error_map_events event
      JOIN provider_error_map_versions version
        ON version.tenant_id=event.tenant_id
       AND version.provider_error_map_version_id=event.provider_error_map_version_id
      JOIN provider_error_maps map
        ON map.tenant_id=event.tenant_id
       AND map.provider_error_map_id=event.provider_error_map_id
       AND map.current_version=event.rule_version
      WHERE event.tenant_id=NEW.tenant_id
        AND event.provider_error_map_id=NEW.object_id
        AND event.actor_user_id=NEW.actor_user_id
        AND event.request_id=NEW.request_id
        AND event.trace_id=NEW.trace_id
        AND (
          (NEW.action='error_map.created' AND event.operation='create')
          OR (NEW.action='error_map.updated' AND event.operation IN ('update','enable','disable'))
        )
    )
  )
)
OR (
  NEW.object_type='provider_error_map'
  AND NEW.action NOT IN ('error_map.created','error_map.updated')
)
OR (
  NEW.action='webhook_dead_letter.archived'
  AND (
    NEW.object_type!='webhook_dead_letter'
    OR NOT EXISTS (
      SELECT 1 FROM webhook_dead_letter_events dead_letter
      WHERE dead_letter.tenant_id=NEW.tenant_id
        AND dead_letter.dead_letter_event_id=NEW.object_id
        AND dead_letter.status='archived'
        AND dead_letter.archived_by=NEW.actor_user_id
    )
  )
)
OR (
  NEW.object_type='webhook_dead_letter'
  AND NEW.action!='webhook_dead_letter.archived'
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_conflict_insert
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM audit_logs current
  WHERE current.audit_log_id=NEW.audit_log_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
    OR (
      NEW.action IN ('policy.dry_run_executed','model_quality_score.updated')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action='finops.adjustment_created'
      AND current.action='finops.adjustment_created'
      AND current.tenant_id=NEW.tenant_id AND current.object_id=NEW.object_id
    )
    OR (
      NEW.action IN (
        'cost_anomaly.acknowledged','cost_anomaly.dismissed',
        'cost_anomaly.temporary_rate_limit_applied',
        'cost_anomaly.temporary_rate_limit_released'
      )
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action IN ('error_map.created','error_map.updated')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;

COMMIT;
