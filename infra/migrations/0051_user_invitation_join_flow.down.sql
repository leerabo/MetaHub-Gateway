-- 回滚职责：仅在没有任何 PRD-060 邀请、审计、Webhook 或导出筛选事实时，
-- 把 0051 精确保真恢复为完整 0050；不得继续回退 PRD-059 角色权限矩阵。
-- 安全边界：一次性 Token、邮箱、用户、AuditLog 和投递证据都不能被静默删除。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd060_down_guard (
  business_fact_count INTEGER NOT NULL,
  unknown_dependency_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd060_down_guard
BEFORE INSERT ON prd060_down_guard FOR EACH ROW
WHEN NEW.business_fact_count!=0 OR NEW.unknown_dependency_count!=0
BEGIN SELECT RAISE(ABORT,'prd060_down_blocked'); END;

-- 在首项持久 DDL 前统计全部业务事实；即使导出结果为零，其冻结筛选条件也属于不可丢证据。
INSERT INTO prd060_down_guard(business_fact_count,unknown_dependency_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0051' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM invitations)
  + (SELECT COUNT(*) FROM invitation_events)
  + (SELECT COUNT(*) FROM audit_logs
      WHERE action IN ('user.invited','invitation.accepted')
         OR object_type='invitation')
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
      WHERE (json_valid(endpoint.event_types_json) AND EXISTS (
        SELECT 1 FROM json_each(endpoint.event_types_json) event_type
        WHERE event_type.value IN ('user.invited','invitation.accepted')
      ))
      OR lower(endpoint.event_types_json) LIKE '%user.invited%'
      OR lower(endpoint.event_types_json) LIKE '%invitation.accepted%')
  + (SELECT COUNT(*) FROM webhook_deliveries
      WHERE event_type IN ('user.invited','invitation.accepted'))
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
      JOIN webhook_deliveries delivery
        ON delivery.tenant_id=attempt.tenant_id
       AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
      WHERE delivery.event_type IN ('user.invited','invitation.accepted'))
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
      WHERE event_type IN ('user.invited','invitation.accepted'))
  + (SELECT COUNT(*) FROM audit_exports export
      WHERE (json_valid(export.filters_json) AND (
        json_extract(export.filters_json,'$.action') IN ('user.invited','invitation.accepted')
        OR json_extract(export.filters_json,'$.object_type')='invitation'
      ))
      OR lower(export.filters_json) LIKE '%user.invited%'
      OR lower(export.filters_json) LIKE '%invitation.accepted%'
      OR lower(export.filters_json) LIKE '%"object_type":"invitation"%'),
  (SELECT COUNT(*) FROM schema_migrations
    WHERE status='applied' AND version>'0051')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.sql IS NOT NULL
        AND (
          lower(object.sql) LIKE '%invitations%'
          OR lower(object.sql) LIKE '%invitation_events%'
          OR lower(object.sql) LIKE '%user.invited%'
          OR lower(object.sql) LIKE '%invitation.accepted%'
        )
        AND object.name NOT IN (
          'invitations','invitation_events','audit_logs',
          'idx_invitations_tenant_time_prd060','idx_invitations_tenant_status_prd060',
          'idx_invitations_pending_email_prd060','idx_audit_logs_invitation_request_unique',
          'idx_invitation_events_tenant_time_prd060',
          'idx_invitation_events_tenant_status_prd060',
          'trg_invitations_owner_insert_prd060','trg_invitations_conflict_insert_prd060',
          'trg_invitations_state_update_prd060','trg_invitations_delete_prd060',
          'trg_invitations_rowid_insert_prd060',
          'trg_invitation_events_owner_insert_prd060',
          'trg_invitation_events_conflict_insert_prd060',
          'trg_invitation_events_state_update_prd060',
          'trg_invitation_events_delete_prd060','trg_invitation_events_rowid_insert_prd060',
          'trg_audit_logs_owner_insert','trg_audit_logs_prd050_conflict_insert'
        ));
DROP TRIGGER trg_prd060_down_guard;
DROP TABLE prd060_down_guard;

-- TEMP 快照只用于本事务逐列保真验证，不成为运行期产品表。
CREATE TEMP TABLE prd060_down_audit_before AS
SELECT rowid AS source_rowid,* FROM audit_logs;
CREATE TEMP TABLE prd060_down_sequences_before AS
SELECT rowid AS source_rowid,* FROM audit_log_export_sequences;
CREATE TEMP TABLE prd060_down_exports_before AS
SELECT rowid AS source_rowid,* FROM audit_exports;
CREATE TEMP TABLE prd060_down_export_events_before AS
SELECT rowid AS source_rowid,* FROM audit_export_events;
CREATE TEMP TABLE prd060_down_role_events_before AS
SELECT rowid AS source_rowid,* FROM role_permission_events;
CREATE TEMP TABLE prd060_down_sqlite_sequence_before AS
SELECT name,seq FROM sqlite_sequence WHERE name='audit_log_export_sequences';

-- 先按子到父顺序删除 0051 空表，再重建 AuditLog；guard 已保证没有业务事实。
DROP TRIGGER trg_invitation_events_conflict_insert_prd060;
DROP TRIGGER trg_invitation_events_owner_insert_prd060;
DROP TRIGGER trg_invitation_events_state_update_prd060;
DROP TRIGGER trg_invitation_events_delete_prd060;
DROP TRIGGER trg_invitation_events_rowid_insert_prd060;
DROP TABLE invitation_events;

DROP TRIGGER trg_invitations_conflict_insert_prd060;
DROP TRIGGER trg_invitations_owner_insert_prd060;
DROP TRIGGER trg_invitations_state_update_prd060;
DROP TRIGGER trg_invitations_delete_prd060;
DROP TRIGGER trg_invitations_rowid_insert_prd060;

-- 所有读取 AuditLog 或 Invitation 的入向 trigger 必须在父表切换前移除。
DROP TRIGGER trg_role_permission_events_owner_insert_prd059;
DROP TRIGGER trg_audit_log_export_sequences_owner_insert_prd058;
DROP TRIGGER trg_audit_exports_conflict_insert_prd058;
DROP TRIGGER trg_audit_exports_owner_insert_prd058;
DROP TRIGGER trg_audit_logs_owner_insert;
DROP TRIGGER trg_audit_logs_immutable_update;
DROP TRIGGER trg_audit_logs_immutable_delete;
DROP TRIGGER trg_audit_logs_prd050_conflict_insert;
DROP TRIGGER trg_audit_logs_prd050_rowid_insert;
DROP TRIGGER trg_audit_logs_export_sequence_insert_prd058;
DROP TABLE invitations;

-- 0050 AuditLog 精确定义：删除两个 Invitation 枚举，但保留六角色和 role.permission.changed。
CREATE TABLE audit_logs_prd060_down (
  audit_log_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (
    actor_role IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  ),
  action TEXT NOT NULL CHECK (
    action IN (
      'provider.created','provider.updated',
      'provider_key.created','provider_key.updated',
      'virtual_key.created','virtual_key.updated',
      'budget_rule.created','budget_rule.updated',
      'dlp_rule.created','dlp_rule.updated',
      'webhook_delivery.replayed','webhook_endpoint.secret_rotated',
      'webhook_dead_letter.archived',
      'usage_export.requested','usage_export.downloaded',
      'policy.created','policy.updated',
      'policy_snapshot.published','policy_snapshot.rolled_back',
      'policy.dry_run_executed',
      'routing_policy.created','routing_policy.updated',
      'model_quality_score.updated',
      'cache_affinity.created','cache_affinity.updated',
      'finops.adjustment_created',
      'cost_anomaly.acknowledged','cost_anomaly.dismissed',
      'cost_anomaly.temporary_rate_limit_applied',
      'cost_anomaly.temporary_rate_limit_released',
      'error_map.created','error_map.updated',
      'audit.exported','role.permission_changed'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider','provider_key','virtual_key','budget_rule','dlp_rule',
      'webhook_delivery','webhook_endpoint','webhook_dead_letter',
      'usage_export_job','policy','policy_snapshot','policy_explanation',
      'routing_policy','model_quality_score','cache_affinity',
      'finops_adjustment','cost_anomaly','provider_error_map','audit_export',
      'role_permission'
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
  UNIQUE (tenant_id,audit_log_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);
-- 回滚同样显式复制全列与 rowid，不能因删除 Invitation 枚举而重排既有 AuditLog。
INSERT INTO audit_logs_prd060_down(
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

CREATE TEMP TABLE prd060_down_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd060_down_audit_copy_guard
BEFORE INSERT ON prd060_down_audit_copy_guard FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0 OR NEW.row_difference_count!=0
BEGIN SELECT RAISE(ABORT,'prd060_down_audit_copy_failed'); END;
-- 在替换正式表前比较行数、NULL 分布和双向全列差异，拒绝任何历史事实漂移。
INSERT INTO prd060_down_audit_copy_guard(
  source_count,copied_count,null_mismatch_count,row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd060_down),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd060_down WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd060_down WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd060_down WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs
    EXCEPT SELECT rowid,* FROM audit_logs_prd060_down
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs_prd060_down
    EXCEPT SELECT rowid,* FROM audit_logs
  ));
DROP TRIGGER trg_prd060_down_audit_copy_guard;
DROP TABLE prd060_down_audit_copy_guard;

DROP TABLE audit_logs;
-- 复制保真门禁通过后才恢复 0050 表名，避免 down 留下半重建结构。
ALTER TABLE audit_logs_prd060_down RENAME TO audit_logs;

-- 恢复 0050 全部 tenant-first 索引和既有幂等键，不保留 Invitation 专用索引。
CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id,created_at DESC,audit_log_id DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id,actor_user_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id,object_type,object_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id,action,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id,request_id);
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id,object_id)
  WHERE action='finops.adjustment_created';
CREATE UNIQUE INDEX idx_audit_logs_role_permission_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='role.permission_changed';

CREATE TRIGGER trg_audit_log_export_sequences_owner_insert_prd058
BEFORE INSERT ON audit_log_export_sequences FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM audit_logs audit
  WHERE audit.tenant_id=NEW.tenant_id AND audit.audit_log_id=NEW.audit_log_id
)
BEGIN SELECT RAISE(ABORT,'audit_log_export_sequence_owner_mismatch'); END;

CREATE TRIGGER trg_audit_exports_owner_insert_prd058
BEFORE INSERT ON audit_exports FOR EACH ROW
WHEN NOT (
  NOT EXISTS (SELECT 1 FROM audit_logs audit WHERE audit.audit_log_id=NEW.audit_log_id)
  AND EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.requested_by
  )
  AND (
    (NEW.snapshot_sequence=0
      AND NEW.snapshot_audit_log_id IS NULL AND NEW.snapshot_created_at IS NULL)
    OR
    (NEW.snapshot_sequence>0
      AND NEW.snapshot_audit_log_id IS NOT NULL AND NEW.snapshot_created_at IS NOT NULL
      AND EXISTS (
        SELECT 1
        FROM audit_log_export_sequences sequence
        JOIN audit_logs audit
          ON audit.tenant_id=sequence.tenant_id
         AND audit.audit_log_id=sequence.audit_log_id
        WHERE sequence.tenant_id=NEW.tenant_id
          AND sequence.audit_log_sequence=NEW.snapshot_sequence
          AND sequence.audit_log_id=NEW.snapshot_audit_log_id
          AND audit.created_at=NEW.snapshot_created_at
      ))
  )
  AND json_valid(NEW.filters_json)
  AND CASE WHEN json_valid(NEW.filters_json) THEN json_type(NEW.filters_json) END='object'
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.filters_json) THEN NEW.filters_json ELSE '{}' END
    ) filter
    WHERE filter.key NOT IN (
      'created_from','created_to','actor_user_id','action','object_type','object_id','request_id'
    ) OR filter.type NOT IN ('text','null')
  )
  AND json_valid(NEW.event_payload_json)
  AND CASE WHEN json_valid(NEW.event_payload_json)
    THEN json_type(NEW.event_payload_json) END='object'
  AND (SELECT COUNT(*) FROM json_each(
    CASE WHEN json_valid(NEW.event_payload_json) THEN NEW.event_payload_json ELSE '{}' END
  ))=8
  AND (SELECT COUNT(DISTINCT key) FROM json_each(
    CASE WHEN json_valid(NEW.event_payload_json) THEN NEW.event_payload_json ELSE '{}' END
  ))=8
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.event_payload_json) THEN NEW.event_payload_json ELSE '{}' END
    ) field
    WHERE field.key NOT IN (
      'audit_export_id','status','format','schema_version','row_count',
      'content_size_bytes','content_sha256','filter_fingerprint'
    )
  )
  AND json_extract(NEW.event_payload_json,'$.audit_export_id')=NEW.audit_export_id
  AND json_extract(NEW.event_payload_json,'$.status')=NEW.status
  AND json_extract(NEW.event_payload_json,'$.format')=NEW.format
  AND json_type(NEW.event_payload_json,'$.schema_version')='integer'
  AND json_extract(NEW.event_payload_json,'$.schema_version')=NEW.schema_version
  AND json_type(NEW.event_payload_json,'$.row_count')='integer'
  AND json_extract(NEW.event_payload_json,'$.row_count')=NEW.row_count
  AND json_type(NEW.event_payload_json,'$.content_size_bytes')='integer'
  AND json_extract(NEW.event_payload_json,'$.content_size_bytes')=NEW.content_size_bytes
  AND json_extract(NEW.event_payload_json,'$.content_sha256')=NEW.content_sha256
  AND json_extract(NEW.event_payload_json,'$.filter_fingerprint')=NEW.filter_fingerprint
)
BEGIN SELECT RAISE(ABORT,'audit_export_owner_mismatch'); END;

CREATE TRIGGER trg_audit_exports_conflict_insert_prd058
BEFORE INSERT ON audit_exports FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM audit_exports current
  WHERE current.audit_export_id=NEW.audit_export_id
     OR current.audit_log_id=NEW.audit_log_id
     OR (current.tenant_id=NEW.tenant_id AND current.requested_by=NEW.requested_by
         AND current.request_id=NEW.request_id)
     OR (current.tenant_id=NEW.tenant_id AND current.event_id=NEW.event_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'audit_export_immutable'); END;

CREATE TRIGGER trg_role_permission_events_owner_insert_prd059
BEFORE INSERT ON role_permission_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.code!='admin' AND role.status='active'
    AND role.permission_revision=NEW.after_revision
    AND role.matrix_sha256=NEW.after_sha256
)
OR NOT EXISTS (
  SELECT 1 FROM users actor
  WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
    AND actor.status='active' AND actor.role=NEW.actor_role
)
OR NOT EXISTS (
  SELECT 1 FROM audit_logs audit
  WHERE audit.tenant_id=NEW.tenant_id AND audit.audit_log_id=NEW.audit_log_id
    AND audit.actor_user_id=NEW.actor_user_id AND audit.actor_role=NEW.actor_role
    AND audit.action='role.permission_changed'
    AND audit.object_type='role_permission' AND audit.object_id=NEW.role_id
    AND audit.request_id=NEW.request_id AND audit.trace_id=NEW.trace_id
    AND audit.created_at=NEW.created_at
    AND json_extract(audit.before_summary_json,'$.permission_revision')=NEW.before_revision
    AND json_extract(audit.before_summary_json,'$.matrix_sha256')=NEW.before_sha256
    AND json_extract(audit.after_summary_json,'$.permission_revision')=NEW.after_revision
    AND json_extract(audit.after_summary_json,'$.matrix_sha256')=NEW.after_sha256
    AND json_extract(audit.after_summary_json,'$.permission_count')=(
      SELECT COUNT(*) FROM role_permissions relation
      WHERE relation.tenant_id=NEW.tenant_id AND relation.role_id=NEW.role_id
    )
    AND json_extract(audit.before_summary_json,'$.permission_count')=(
      SELECT COUNT(*) FROM role_permissions relation
      WHERE relation.tenant_id=NEW.tenant_id AND relation.role_id=NEW.role_id
    )-(SELECT COUNT(*) FROM json_each(NEW.added_permissions_json))
     +(SELECT COUNT(*) FROM json_each(NEW.removed_permissions_json))
)
OR NEW.delivery_status!='pending' OR NEW.last_error_code IS NOT NULL
OR NEW.dispatched_at IS NOT NULL OR NEW.updated_at!=NEW.created_at
OR (SELECT COUNT(*) FROM json_each(NEW.added_permissions_json))
  +(SELECT COUNT(*) FROM json_each(NEW.removed_permissions_json))=0
OR EXISTS (
  SELECT 1 FROM json_each(NEW.added_permissions_json) item
  WHERE item.type!='text'
    OR NOT EXISTS (
      SELECT 1
      FROM rbac_role_permission_templates role_template
      JOIN roles role ON role.code=role_template.role_code
      WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
        AND role_template.permission_code=item.value
    )
    OR NOT EXISTS (
      SELECT 1
      FROM role_permissions relation
      JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=NEW.tenant_id AND relation.role_id=NEW.role_id
        AND permission.code=item.value
    )
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.removed_permissions_json) item
  WHERE item.type!='text'
    OR NOT EXISTS (
      SELECT 1
      FROM rbac_role_permission_templates role_template
      JOIN roles role ON role.code=role_template.role_code
      WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
        AND role_template.permission_code=item.value
    )
    OR EXISTS (
      SELECT 1
      FROM role_permissions relation
      JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=NEW.tenant_id AND relation.role_id=NEW.role_id
        AND permission.code=item.value
    )
)
OR (SELECT COUNT(*) FROM json_each(NEW.added_permissions_json))
  !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.added_permissions_json))
OR (SELECT COUNT(*) FROM json_each(NEW.removed_permissions_json))
  !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.removed_permissions_json))
OR EXISTS (
  SELECT 1 FROM json_each(NEW.added_permissions_json) added
  JOIN json_each(NEW.removed_permissions_json) removed ON removed.value=added.value
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.added_permissions_json) item
  JOIN json_each(NEW.added_permissions_json) next ON next.key=item.key+1
  WHERE CAST(item.value AS TEXT)>=CAST(next.value AS TEXT)
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.removed_permissions_json) item
  JOIN json_each(NEW.removed_permissions_json) next ON next.key=item.key+1
  WHERE CAST(item.value AS TEXT)>=CAST(next.value AS TEXT)
)
OR (SELECT COUNT(*) FROM json_each(NEW.payload_json))!=9
OR EXISTS (
  SELECT 1 FROM json_each(NEW.payload_json) field
  WHERE field.key NOT IN (
    'schema_version','role_id','role_code','before_revision','after_revision',
    'before_sha256','after_sha256','added_permissions','removed_permissions'
  )
)
OR json_extract(NEW.payload_json,'$.schema_version')!=1
OR json_extract(NEW.payload_json,'$.role_id')!=NEW.role_id
OR json_extract(NEW.payload_json,'$.before_revision')!=NEW.before_revision
OR json_extract(NEW.payload_json,'$.after_revision')!=NEW.after_revision
OR json_extract(NEW.payload_json,'$.before_sha256')!=NEW.before_sha256
OR json_extract(NEW.payload_json,'$.after_sha256')!=NEW.after_sha256
OR json(json_extract(NEW.payload_json,'$.added_permissions'))
  !=json(NEW.added_permissions_json)
OR json(json_extract(NEW.payload_json,'$.removed_permissions'))
  !=json(NEW.removed_permissions_json)
OR NOT EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.code=json_extract(NEW.payload_json,'$.role_code')
)
OR EXISTS (
  SELECT 1 FROM role_permission_events current
  WHERE current.event_id=NEW.event_id
     OR (current.tenant_id=NEW.tenant_id AND current.actor_user_id=NEW.actor_user_id
         AND current.request_id=NEW.request_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_event_invalid'); END;

-- 恢复 0050 AuditLog owner 的全部既有分支，不保留任何 Invitation 特例。
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
OR (
  NEW.action='audit.exported'
  AND (
    NEW.object_type!='audit_export'
    OR NOT EXISTS (
      SELECT 1 FROM audit_exports export
      WHERE export.tenant_id=NEW.tenant_id
        AND export.audit_export_id=NEW.object_id
        AND export.audit_log_id=NEW.audit_log_id
        AND export.requested_by=NEW.actor_user_id
        AND export.request_id=NEW.request_id
        AND export.trace_id=NEW.trace_id
        AND export.created_at=NEW.created_at
    )
  )
)
OR (NEW.object_type='audit_export' AND NEW.action!='audit.exported')
OR (
  EXISTS (SELECT 1 FROM audit_exports export WHERE export.audit_log_id=NEW.audit_log_id)
  AND NOT (
    NEW.action='audit.exported' AND NEW.object_type='audit_export'
    AND EXISTS (
      SELECT 1 FROM audit_exports export
      WHERE export.tenant_id=NEW.tenant_id
        AND export.audit_export_id=NEW.object_id
        AND export.audit_log_id=NEW.audit_log_id
        AND export.requested_by=NEW.actor_user_id
        AND export.request_id=NEW.request_id
        AND export.trace_id=NEW.trace_id
        AND export.created_at=NEW.created_at
    )
  )
)
OR (
  NEW.action='role.permission_changed'
  AND (
    NEW.object_type!='role_permission'
    OR NOT EXISTS (
      SELECT 1 FROM roles role
      WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.object_id
        AND role.code!='admin' AND role.status='active'
        AND role.permission_revision=json_extract(NEW.after_summary_json,'$.permission_revision')
        AND role.matrix_sha256=json_extract(NEW.after_summary_json,'$.matrix_sha256')
        AND json_extract(NEW.before_summary_json,'$.permission_revision')+1=role.permission_revision
    )
  )
)
OR (NEW.object_type='role_permission' AND NEW.action!='role.permission_changed')
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

CREATE TRIGGER trg_audit_logs_export_sequence_insert_prd058
AFTER INSERT ON audit_logs FOR EACH ROW
BEGIN
  -- 恢复 0050 后仍要求每条 AuditLog 同事务获得唯一稳定导出水位。
  INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id)
  VALUES(NEW.tenant_id,NEW.audit_log_id);
END;

-- 提交前验证 PRD-060 对象已消失、0050 对象完整，且五张历史表与序列逐列保真。
CREATE TEMP TABLE prd060_down_final_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd060_down_final_guard
BEFORE INSERT ON prd060_down_final_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd060_down_final_schema_invalid'); END;
-- 最终统一证明 PRD-060 对象已消失、0050 guard 完整且五张历史表与外键全部保真。
INSERT INTO prd060_down_final_guard(invalid_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE name IN ('invitations','invitation_events',
        'idx_audit_logs_invitation_request_unique'))=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'audit_logs','audit_log_export_sequences','audit_exports',
        'audit_export_events','role_permission_events'
      ))=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN (
        'trg_audit_log_export_sequences_owner_insert_prd058',
        'trg_audit_exports_owner_insert_prd058','trg_audit_exports_conflict_insert_prd058',
        'trg_role_permission_events_owner_insert_prd059','trg_audit_logs_owner_insert',
        'trg_audit_logs_immutable_update','trg_audit_logs_immutable_delete',
        'trg_audit_logs_prd050_conflict_insert','trg_audit_logs_prd050_rowid_insert',
        'trg_audit_logs_export_sequence_insert_prd058'
      ))=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs)
      =(SELECT COUNT(*) FROM audit_log_export_sequences) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_down_audit_before
      EXCEPT SELECT rowid,* FROM audit_logs
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT * FROM prd060_down_audit_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_down_sequences_before
      EXCEPT SELECT rowid,* FROM audit_log_export_sequences
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_log_export_sequences
      EXCEPT SELECT * FROM prd060_down_sequences_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_down_exports_before
      EXCEPT SELECT rowid,* FROM audit_exports
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_exports
      EXCEPT SELECT * FROM prd060_down_exports_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_down_export_events_before
      EXCEPT SELECT rowid,* FROM audit_export_events
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_export_events
      EXCEPT SELECT * FROM prd060_down_export_events_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_down_role_events_before
      EXCEPT SELECT rowid,* FROM role_permission_events
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM role_permission_events
      EXCEPT SELECT * FROM prd060_down_role_events_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_down_sqlite_sequence_before
      EXCEPT SELECT name,seq FROM sqlite_sequence
      WHERE name='audit_log_export_sequences'
    ))
  + (SELECT COUNT(*) FROM (
      SELECT name,seq FROM sqlite_sequence
      WHERE name='audit_log_export_sequences'
      EXCEPT SELECT * FROM prd060_down_sqlite_sequence_before
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check);
DROP TRIGGER trg_prd060_down_final_guard;
DROP TABLE prd060_down_final_guard;

DROP TABLE prd060_down_audit_before;
DROP TABLE prd060_down_sequences_before;
DROP TABLE prd060_down_exports_before;
DROP TABLE prd060_down_export_events_before;
DROP TABLE prd060_down_role_events_before;
DROP TABLE prd060_down_sqlite_sequence_before;

COMMIT;
PRAGMA foreign_keys = ON;
