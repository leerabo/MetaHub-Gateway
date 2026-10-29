-- 回滚职责：只允许没有任何 PRD-056 业务、审计、Webhook 或后续 schema 依赖的空事实库回到 0045。
-- 原子性边界：全部证据和 sqlite_master/foreign_key 依赖扫描必须先于第一条持久 DDL。
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd056_down_guard (blocker_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd056_down_guard
BEFORE INSERT ON prd056_down_guard FOR EACH ROW
WHEN NEW.blocker_count!=0
BEGIN SELECT RAISE(ABORT,'prd056_down_dependency_exists'); END;

-- 回滚前汇总全部业务事实和外部依赖，非零 blocker 会由临时触发器原子拒绝。
INSERT INTO prd056_down_guard(blocker_count)
SELECT
  (SELECT COUNT(*) FROM provider_error_maps)
  +(SELECT COUNT(*) FROM provider_error_map_versions)
  +(SELECT COUNT(*) FROM provider_error_map_heads)
  +(SELECT COUNT(*) FROM provider_error_map_head_versions)
  +(SELECT COUNT(*) FROM provider_error_map_events)
  +(SELECT COUNT(*) FROM provider_error_map_operations)
  +(SELECT COUNT(*) FROM provider_error_facts)
  +(SELECT COUNT(*) FROM audit_logs
      WHERE action IN ('error_map.created','error_map.updated')
         OR object_type='provider_error_map')
  +(SELECT COUNT(*) FROM webhook_endpoints endpoint
      WHERE EXISTS (
        SELECT 1 FROM json_each(
          CASE WHEN json_valid(endpoint.event_types_json)
            THEN endpoint.event_types_json ELSE '[]' END
        ) event_type
        WHERE event_type.value='error_map.updated'
      ))
  +(SELECT COUNT(*) FROM webhook_deliveries delivery
      WHERE delivery.event_type='error_map.updated')
  +(SELECT COUNT(*)
      FROM webhook_delivery_attempts attempt
      JOIN webhook_deliveries delivery
        ON delivery.tenant_id=attempt.tenant_id
       AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
      WHERE delivery.event_type='error_map.updated')
  +CASE WHEN (
      SELECT COUNT(*) FROM provider_error_builtin_catalog_versions
    )=1
    AND EXISTS (
      SELECT 1 FROM provider_error_builtin_catalog_versions
      WHERE builtin_catalog_version='provider-error-builtin-v1'
        AND catalog_hash='a209e9d5e86e5b08d6fe97a1d0475ad8533704f6f782fa20dd732fc4dafdc507'
        AND created_at='2026-07-29T00:00:00Z'
    ) THEN 0 ELSE 1 END
  +CASE WHEN (
      SELECT COUNT(*) FROM provider_error_builtin_rule_versions
    )=13
    AND (
      SELECT COUNT(*) FROM provider_error_builtin_rule_versions
      WHERE builtin_catalog_version='provider-error-builtin-v1'
        AND error_map_id IN (
          'guard_transport_timeout','guard_transport_unavailable','guard_http_auth',
          'guard_http_timeout','guard_http_5xx','builtin_quota_signal',
          'builtin_model_not_found_signal','builtin_http_402','builtin_http_404',
          'builtin_http_429','builtin_http_400_422','builtin_http_other_4xx',
          'builtin_response_default'
        )
    )=13 THEN 0 ELSE 1 END
  +(SELECT COUNT(*)
    FROM sqlite_master object
    WHERE object.sql IS NOT NULL
      AND (
        lower(object.sql) LIKE '%provider_error_map%'
        OR lower(object.sql) LIKE '%provider_error_fact%'
        OR lower(object.sql) LIKE '%provider_error_builtin_%'
        OR (
          lower(object.sql) LIKE '%stream_sessions%'
          AND (
            lower(object.sql) LIKE '%upstream_http_status%'
            OR lower(object.sql) LIKE '%retryable%'
          )
        )
      )
      AND object.name NOT IN (
        'provider_error_maps','provider_error_map_versions','provider_error_map_heads',
        'provider_error_map_head_versions','provider_error_map_events',
        'provider_error_map_operations','provider_error_facts',
        'provider_error_builtin_catalog_versions','provider_error_builtin_rule_versions',
        'audit_logs','stream_sessions',
        'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
        'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
        'trg_audit_logs_prd050_rowid_insert',
        'idx_users_tenant_user_owner_prd056',
        'idx_providers_tenant_provider_owner_prd056',
        'idx_models_tenant_model_owner_prd056',
        'idx_routing_attempts_tenant_owner_prd056',
        'idx_stream_sessions_tenant_owner_prd056',
        'trg_routing_attempt_provider_error_fact_update_prd056',
        'trg_routing_attempt_provider_error_fact_delete_prd056',
        'trg_routing_attempt_provider_error_fact_conflict_insert_prd056',
        'trg_stream_session_provider_error_fact_update_prd056',
        'trg_stream_session_provider_error_fact_delete_prd056',
        'trg_stream_session_provider_error_fact_conflict_insert_prd056'
      )
      AND object.name NOT GLOB 'trg_provider_error_*_prd056'
      AND object.name NOT GLOB 'idx_provider_error_*_prd056')
  +(SELECT COUNT(*) FROM (
    SELECT owner.name, fk.id, fk."table"
    FROM sqlite_master owner
    JOIN pragma_foreign_key_list(owner.name) fk
    WHERE owner.type='table'
      AND owner.name NOT IN (
        'provider_error_maps','provider_error_map_versions','provider_error_map_heads',
        'provider_error_map_head_versions','provider_error_map_events',
        'provider_error_map_operations','provider_error_facts',
        'provider_error_builtin_catalog_versions','provider_error_builtin_rule_versions'
      )
      AND (
        fk."table" IN (
          'provider_error_maps','provider_error_map_versions','provider_error_map_heads',
          'provider_error_map_head_versions','provider_error_map_events',
          'provider_error_map_operations','provider_error_facts',
          'provider_error_builtin_catalog_versions','provider_error_builtin_rule_versions'
        )
        OR fk."table" IN ('routing_attempts','stream_sessions')
      )
    GROUP BY owner.name, fk.id, fk."table"
    HAVING fk."table" LIKE 'provider_error_%'
      OR (
        fk."table"='routing_attempts'
        AND SUM(fk."from"='tenant_id' AND fk."to"='tenant_id')>0
        AND SUM(fk."from"='routing_attempt_id' AND fk."to"='routing_attempt_id')>0
      )
      OR (
        fk."table"='stream_sessions'
        AND SUM(fk."from"='tenant_id' AND fk."to"='tenant_id')>0
        AND SUM(fk."from"='stream_session_id' AND fk."to"='stream_session_id')>0
      )
  ));

DROP TRIGGER trg_prd056_down_guard;
DROP TABLE prd056_down_guard;

-- 先移除 Audit 对 Source Event 的引用，再删除 PRD-056 表族；rowid 与历史 17 列逐值保真。
CREATE TABLE audit_logs_prd056_down (
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
      'finops.adjustment_created',
      'cost_anomaly.acknowledged', 'cost_anomaly.dismissed',
      'cost_anomaly.temporary_rate_limit_applied',
      'cost_anomaly.temporary_rate_limit_released'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot', 'policy_explanation', 'routing_policy',
      'model_quality_score', 'cache_affinity', 'finops_adjustment', 'cost_anomaly'
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

-- 回滚 Audit 表时保留 rowid 与全部原值，不能因移除 PRD-056 对象类型而丢失历史审计。
INSERT INTO audit_logs_prd056_down(
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

CREATE TEMP TABLE prd056_audit_down_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd056_audit_down_copy_guard
BEFORE INSERT ON prd056_audit_down_copy_guard FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0 OR NEW.row_difference_count!=0
BEGIN SELECT RAISE(ABORT,'prd056_audit_down_copy_failed'); END;
-- 旧结构恢复前再次核对行数、NULL 分布和双向差集，复制不保真则整笔回滚。
INSERT INTO prd056_audit_down_copy_guard(
  source_count,copied_count,null_mismatch_count,row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd056_down),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd056_down WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd056_down WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd056_down WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs
    EXCEPT SELECT rowid,* FROM audit_logs_prd056_down
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs_prd056_down
    EXCEPT SELECT rowid,* FROM audit_logs
  ));
DROP TRIGGER trg_prd056_audit_down_copy_guard;
DROP TABLE prd056_audit_down_copy_guard;
DROP TABLE audit_logs;
-- 保真门禁通过后才把旧约束 Audit 表恢复为正式名称，避免留下半切换 schema。
ALTER TABLE audit_logs_prd056_down RENAME TO audit_logs;

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
      SELECT 1
      FROM cost_anomaly_actions action
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
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;

-- 所有 PRD-056 owner 冻结触发器必须先移除，随后按 FK 子到父顺序删除空事实表族。
DROP TRIGGER trg_routing_attempt_provider_error_fact_update_prd056;
DROP TRIGGER trg_routing_attempt_provider_error_fact_delete_prd056;
DROP TRIGGER trg_routing_attempt_provider_error_fact_conflict_insert_prd056;
DROP TRIGGER trg_stream_session_provider_error_fact_update_prd056;
DROP TRIGGER trg_stream_session_provider_error_fact_delete_prd056;
DROP TRIGGER trg_stream_session_provider_error_fact_conflict_insert_prd056;

DROP TABLE provider_error_facts;
DROP TABLE provider_error_map_operations;
DROP TABLE provider_error_map_events;
DROP TABLE provider_error_map_heads;
DROP TABLE provider_error_map_head_versions;
DROP TABLE provider_error_maps;
DROP TABLE provider_error_map_versions;
DROP TABLE provider_error_builtin_rule_versions;
DROP TABLE provider_error_builtin_catalog_versions;

DROP INDEX idx_routing_attempts_tenant_owner_prd056;
DROP INDEX idx_stream_sessions_tenant_owner_prd056;

-- SQLite DROP COLUMN 仅在所有索引、触发器和 FK 依赖清除后执行，避免留下半套 stream schema。
ALTER TABLE stream_sessions DROP COLUMN retryable;
-- 最后移除冻结 HTTP 状态列，至此 StreamSession 才完整恢复到 0045 结构。
ALTER TABLE stream_sessions DROP COLUMN upstream_http_status;

DROP INDEX idx_models_tenant_model_owner_prd056;
DROP INDEX idx_providers_tenant_provider_owner_prd056;
DROP INDEX idx_users_tenant_user_owner_prd056;

COMMIT;
