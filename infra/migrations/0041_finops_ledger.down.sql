-- PRD-051 条件回滚：只允许删除可由 Usage 重建的投影，绝不静默删除人工财务事实或投递事实。
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- 结构门禁先确认当前确为完整 0041；TEMP guard 不改变持久 schema。
CREATE TEMP TABLE prd051_down_prerequisite_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd051_down_prerequisite_guard
BEFORE INSERT ON prd051_down_prerequisite_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd051_down_prerequisite_missing');
END;
-- 只有完整 0041/0040 版本链和预期依赖对象同时存在时才进入条件回滚。
INSERT INTO prd051_down_prerequisite_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM schema_migrations
    WHERE version='0041' AND status='applied'
  )=1 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM schema_migrations
    WHERE version='0040' AND status='applied'
  )=1 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='table' AND name IN (
      'finops_adjustments', 'finops_ledger_entries', 'audit_logs',
      'usage_ledger', 'price_snapshots', 'webhook_endpoints',
      'webhook_deliveries', 'webhook_delivery_attempts'
    )
  )=8 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND name IN (
      'trg_audit_logs_owner_insert', 'trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete', 'trg_audit_logs_prd050_conflict_insert',
      'trg_audit_logs_prd050_rowid_insert',
      'trg_finops_adjustments_owner_insert_prd051',
      'trg_finops_adjustments_conflict_insert_prd051',
      'trg_finops_adjustments_rowid_insert_prd051',
      'trg_finops_adjustments_immutable_update_prd051',
      'trg_finops_adjustments_immutable_delete_prd051',
      'trg_finops_ledger_owner_insert_prd051',
      'trg_finops_ledger_conflict_insert_prd051',
      'trg_finops_ledger_immutable_update_prd051',
      'trg_finops_ledger_immutable_delete_prd051',
      'trg_price_snapshots_owner_insert_prd051',
      'trg_price_snapshots_conflict_insert_prd051',
      'trg_price_snapshots_rowid_insert_prd051',
      'trg_price_snapshots_immutable_update_prd051',
      'trg_price_snapshots_immutable_delete_prd051',
      'trg_usage_ledger_finops_owner_insert_prd051',
      'trg_usage_ledger_finops_amount_insert_prd051',
      'trg_usage_ledger_finops_conflict_insert_prd051',
      'trg_usage_ledger_finops_projection_insert_prd051'
    )
  )=23 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND tbl_name='audit_logs' AND sql IS NOT NULL
  )=8 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND tbl_name='audit_logs'
  )=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='view' AND lower(COALESCE(sql, '')) LIKE '%audit_logs%')
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='trigger' AND tbl_name!='audit_logs'
       AND lower(COALESCE(sql, '')) LIKE '%audit_logs%')
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='table' AND name!='audit_logs'
       AND lower(COALESCE(sql, '')) LIKE '%references%audit_logs%');
DROP TRIGGER trg_prd051_down_prerequisite_guard;
DROP TABLE prd051_down_prerequisite_guard;

-- 不可逆事实门禁必须位于所有持久 DDL 前：任一项存在即原子拒绝整个回滚。
CREATE TEMP TABLE prd051_down_business_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd051_down_business_guard
BEFORE INSERT ON prd051_down_business_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd051_down_business_facts_present');
END;
-- 人工调整、非 Usage 分录、审计或投递任一存在时都拒绝回滚，禁止删除不可逆财务事实。
INSERT INTO prd051_down_business_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM finops_adjustments)
  + (SELECT COUNT(*) FROM finops_ledger_entries WHERE entry_type!='usage_charge')
  + (SELECT COUNT(*) FROM audit_logs
     WHERE action='finops.adjustment_created' OR object_type='finops_adjustment')
  + (SELECT COUNT(*)
     FROM webhook_endpoints endpoint
     WHERE CASE
       WHEN json_valid(endpoint.event_types_json)
        AND json_type(endpoint.event_types_json)='array'
       THEN EXISTS (
         SELECT 1 FROM json_each(endpoint.event_types_json) event_type
         WHERE event_type.type='text'
           AND event_type.value IN ('ledger.entry_created', 'billing.ledger_created')
       )
       ELSE 0
     END)
  + (SELECT COUNT(*)
     FROM webhook_deliveries delivery
     WHERE delivery.event_type IN ('ledger.entry_created', 'billing.ledger_created')
        OR EXISTS (
          SELECT 1 FROM finops_ledger_entries entry
          WHERE entry.tenant_id=delivery.tenant_id AND entry.event_id=delivery.event_id
        ))
  + (SELECT COUNT(*)
     FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type IN ('ledger.entry_created', 'billing.ledger_created')
        OR EXISTS (
          SELECT 1 FROM finops_ledger_entries entry
          WHERE entry.tenant_id=delivery.tenant_id AND entry.event_id=delivery.event_id
        ));
DROP TRIGGER trg_prd051_down_business_guard;
DROP TABLE prd051_down_business_guard;

-- 停止未来 Usage 投影后再清理现有可重建分录，避免回滚事务内产生新正式分录。
DROP TRIGGER trg_usage_ledger_finops_projection_insert_prd051;
DROP TRIGGER trg_usage_ledger_finops_conflict_insert_prd051;
DROP TRIGGER trg_usage_ledger_finops_amount_insert_prd051;
DROP TRIGGER trg_usage_ledger_finops_owner_insert_prd051;

-- PriceSnapshot 的 0041 不可变守卫必须先移除，才能删除仅由 ledger 引用的合成证据。
DROP TRIGGER trg_price_snapshots_immutable_delete_prd051;
DROP TRIGGER trg_price_snapshots_immutable_update_prd051;
DROP TRIGGER trg_price_snapshots_rowid_insert_prd051;
DROP TRIGGER trg_price_snapshots_conflict_insert_prd051;
DROP TRIGGER trg_price_snapshots_owner_insert_prd051;

DROP TRIGGER trg_finops_ledger_immutable_delete_prd051;
DROP TRIGGER trg_finops_ledger_immutable_update_prd051;
DROP TRIGGER trg_finops_ledger_conflict_insert_prd051;
DROP TRIGGER trg_finops_ledger_owner_insert_prd051;
DROP TRIGGER trg_finops_adjustments_immutable_delete_prd051;
DROP TRIGGER trg_finops_adjustments_immutable_update_prd051;
DROP TRIGGER trg_finops_adjustments_rowid_insert_prd051;
DROP TRIGGER trg_finops_adjustments_conflict_insert_prd051;
DROP TRIGGER trg_finops_adjustments_owner_insert_prd051;

-- 先移除 Usage formal entries，再删除只供 ledger 使用且没有被 Usage 自身引用的合成快照。
DROP TABLE finops_ledger_entries;
DROP TABLE finops_adjustments;
-- 仅清理由 0041 为账本恢复创建且未被 Usage 引用的合成快照，保留所有业务价格事实。
DELETE FROM price_snapshots
WHERE source IN ('finops_unpriced', 'finops_legacy_backfill')
  AND json_valid(billing_dimensions)
  AND NULLIF(json_extract(billing_dimensions, '$.finops_usage_ledger_id'), '') IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM usage_ledger usage
    WHERE usage.price_snapshot_id=price_snapshots.price_snapshot_id
  );

DROP INDEX uq_price_snapshots_finops_usage_prd051;
DROP INDEX uq_applications_tenant_application_prd051;
DROP INDEX uq_projects_tenant_project_prd051;
DROP INDEX uq_environments_tenant_environment_prd051;
DROP INDEX uq_virtual_keys_tenant_key_prd051;
DROP INDEX uq_providers_tenant_provider_prd051;
DROP INDEX uq_models_tenant_model_prd051;
DROP INDEX uq_price_snapshots_tenant_snapshot_prd051;
DROP INDEX uq_usage_ledger_tenant_usage_prd051;
DROP INDEX uq_users_tenant_user_prd051;

-- 0041 Audit triggers 引用了 Adjustment；先显式移除，再保真重建 0040 枚举。
DROP TRIGGER trg_audit_logs_prd050_rowid_insert;
DROP TRIGGER trg_audit_logs_prd050_conflict_insert;
DROP TRIGGER trg_audit_logs_immutable_delete;
DROP TRIGGER trg_audit_logs_immutable_update;
DROP TRIGGER trg_audit_logs_owner_insert;

CREATE TABLE audit_logs_prd051_old (
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
      'cache_affinity.created', 'cache_affinity.updated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot', 'policy_explanation', 'routing_policy',
      'model_quality_score', 'cache_affinity'
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
-- Audit 降级复制继续保留 rowid 和全字段，不因移除 FinOps 枚举而改写 0040 历史。
INSERT INTO audit_logs_prd051_old(
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

CREATE TEMP TABLE prd051_down_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd051_down_audit_copy_guard
BEFORE INSERT ON prd051_down_audit_copy_guard
FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0
  OR NEW.row_difference_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd051_down_audit_copy_failed');
END;
-- 切表前执行计数、NULL 分布和双向行差检查，任何不一致都原子终止回滚。
INSERT INTO prd051_down_audit_copy_guard(
  source_count, copied_count, null_mismatch_count, row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd051_old),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd051_old WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd051_old WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd051_old WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid, * FROM audit_logs
    EXCEPT SELECT rowid, * FROM audit_logs_prd051_old
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid, * FROM audit_logs_prd051_old
    EXCEPT SELECT rowid, * FROM audit_logs
  ));
DROP TRIGGER trg_prd051_down_audit_copy_guard;
DROP TABLE prd051_down_audit_copy_guard;
DROP TABLE audit_logs;
-- 保真门禁通过后才恢复 0040 表名，避免中途暴露不完整审计结构。
ALTER TABLE audit_logs_prd051_old RENAME TO audit_logs;

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

CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users user
  WHERE user.user_id=NEW.actor_user_id AND user.tenant_id=NEW.tenant_id
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

-- 回滚终态必须与 0040 对象合同一致，且不残留 0041 表、trigger、index 或自增序列。
CREATE TEMP TABLE prd051_down_integrity_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd051_down_integrity_guard
BEFORE INSERT ON prd051_down_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd051_down_integrity_failed');
END;
-- 最终确认 FinOps 对象已移除且 0040 Audit、外键和完整性全部恢复，再允许提交。
INSERT INTO prd051_down_integrity_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM sqlite_master
   WHERE type='table' AND name IN ('finops_adjustments', 'finops_ledger_entries'))
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE (type='trigger' OR type='index') AND name LIKE '%prd051%')
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND tbl_name='audit_logs' AND sql IS NOT NULL
      AND name IN (
        'idx_audit_logs_tenant_time', 'idx_audit_logs_tenant_actor_time',
        'idx_audit_logs_tenant_object_time', 'idx_audit_logs_tenant_action_time',
        'idx_audit_logs_tenant_request', 'idx_audit_logs_dry_run_request_unique',
        'idx_audit_logs_model_quality_request_unique'
      )
  )=7 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND tbl_name='audit_logs' AND sql IS NOT NULL
  )=7 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND name IN (
      'trg_audit_logs_owner_insert', 'trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete', 'trg_audit_logs_prd050_conflict_insert',
      'trg_audit_logs_prd050_rowid_insert'
    )
  )=5 THEN 0 ELSE 1 END
  + CASE WHEN EXISTS (
    SELECT 1 FROM sqlite_master
    WHERE type='table' AND name='audit_logs'
      AND sql NOT LIKE '%finops.adjustment_created%'
      AND sql NOT LIKE '%finops_adjustment%'
  ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_sequence WHERE name='finops_ledger_entries')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER trg_prd051_down_integrity_guard;
DROP TABLE prd051_down_integrity_guard;

COMMIT;
PRAGMA foreign_keys = ON;
