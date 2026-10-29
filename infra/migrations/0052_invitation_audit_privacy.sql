-- PRD-060 修正迁移：接受邀请的 AuditLog 不再复制个人邮箱，只保留固定脱敏标记。
-- 迁移边界：0051 已经应用后不得改写其 checksum；本迁移先证明旧 User、Invitation、
-- AuditLog 图完整，再执行唯一允许的数据变化，并恢复不可变与冲突优先级约束。
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd060_privacy_up_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd060_privacy_up_guard
BEFORE INSERT ON prd060_privacy_up_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd060_privacy_up_graph_invalid'); END;

-- 首项持久 DDL 前验证迁移版本、0051 核心对象和全部历史接受图；任何残缺关系都原子阻断。
INSERT INTO prd060_privacy_up_guard(invalid_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0051' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations
      WHERE status='applied' AND version>'0051')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'tenants','users','roles','permissions','role_permissions',
        'invitations','invitation_events','audit_logs'
      ))=8
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN (
        'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
        'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
        'trg_audit_logs_prd050_rowid_insert',
        'trg_invitations_owner_insert_prd060','trg_invitations_conflict_insert_prd060',
        'trg_invitations_state_update_prd060','trg_invitations_delete_prd060',
        'trg_invitations_rowid_insert_prd060',
        'trg_invitation_events_owner_insert_prd060',
        'trg_invitation_events_conflict_insert_prd060',
        'trg_invitation_events_state_update_prd060',
        'trg_invitation_events_delete_prd060',
        'trg_invitation_events_rowid_insert_prd060'
      ))=15 THEN 0 ELSE 1 END
;

-- 先通过对象存在性门禁，再准备引用这些表的图校验 SQL；缺表也必须返回统一迁移 guard，
-- 不能在 SQLite 解析 JOIN 时才泄露底层 `no such table` 差异。
INSERT INTO prd060_privacy_up_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*)
      FROM audit_logs audit
      WHERE audit.action='invitation.accepted'
        AND NOT EXISTS (
          SELECT 1
          FROM invitations invitation
          JOIN users actor
            ON actor.tenant_id=invitation.tenant_id
           AND actor.user_id=audit.actor_user_id
          WHERE invitation.tenant_id=audit.tenant_id
            AND invitation.invitation_id=audit.object_id
            AND audit.object_type='invitation' AND audit.object_name IS NULL
            -- 历史接受之后允许用户停用、改角色、改邮箱或租户/角色停用；隐私升级只能
            -- 复核 Invitation 的不可变身份快照，不能用当前 User 状态阻止旧 PII 脱敏。
            AND audit.actor_email=invitation.email
            AND audit.actor_role=invitation.role_code
            AND invitation.status='accepted'
            AND invitation.accepted_user_id=audit.actor_user_id
            AND invitation.accepted_at=audit.created_at
            AND (SELECT COUNT(*) FROM json_each(audit.before_summary_json))=5
            AND (SELECT COUNT(*) FROM json_each(audit.after_summary_json))=5
            AND NOT EXISTS (
              SELECT 1 FROM json_each(audit.before_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND NOT EXISTS (
              SELECT 1 FROM json_each(audit.after_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND json_extract(audit.before_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(audit.before_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(audit.before_summary_json,'$.status')='pending'
            AND json_extract(audit.before_summary_json,'$.expires_at')=invitation.expires_at
            AND json_type(audit.before_summary_json,'$.accepted_user_id')='null'
            AND json_extract(audit.after_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(audit.after_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(audit.after_summary_json,'$.status')='accepted'
            AND json_extract(audit.after_summary_json,'$.expires_at')=invitation.expires_at
            AND json_extract(audit.after_summary_json,'$.accepted_user_id')
              =invitation.accepted_user_id
            AND json(audit.changed_fields_json)=json('["accepted_user_id","status"]')
        ))
  + (SELECT COUNT(*)
      FROM invitations invitation
      WHERE invitation.status='accepted'
        AND (SELECT COUNT(*) FROM audit_logs audit
             WHERE audit.tenant_id=invitation.tenant_id
               AND audit.action='invitation.accepted'
               AND audit.object_type='invitation'
               AND audit.object_id=invitation.invitation_id)!=1);
DROP TRIGGER trg_prd060_privacy_up_guard;
DROP TABLE prd060_privacy_up_guard;

-- 预先生成迁移后唯一合法快照；最终用双向 EXCEPT 证明除接受审计邮箱外没有列或 rowid 漂移。
CREATE TEMP TABLE prd060_privacy_audit_expected AS
SELECT
  rowid AS source_rowid,audit_log_id,tenant_id,actor_user_id,
  CASE WHEN action='invitation.accepted'
    THEN 'invitation-acceptor@redacted.invalid' ELSE actor_email END AS actor_email,
  actor_role,action,object_type,object_id,object_name,before_summary_json,
  after_summary_json,changed_fields_json,client_ip,user_agent,request_id,trace_id,created_at
FROM audit_logs;

-- owner 与 conflict 必须成对重建：SQLite 后创建的 BEFORE trigger 先执行，故 conflict
-- 仍在 owner 之前返回不可变冲突；临时移除 update guard 只用于本事务不可逆脱敏。
DROP TRIGGER trg_audit_logs_prd050_conflict_insert;
DROP TRIGGER trg_audit_logs_owner_insert;
DROP TRIGGER trg_audit_logs_immutable_update;

-- 只改写接受审计的邮箱副本；User 与 Invitation 中履行业务所需的真实邮箱保持不变。
UPDATE audit_logs
SET actor_email='invitation-acceptor@redacted.invalid'
WHERE action='invitation.accepted';

-- 除 Invitation 分支外逐字保留 0051 owner 规则；普通邀请记录真实管理员邮箱，
-- 接受审计只保存固定标记，但仍通过同租户 User 与 Invitation 的真实邮箱关系验明归属。
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
OR (
  NEW.action IN ('user.invited','invitation.accepted')
  AND (
    NEW.object_type!='invitation' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM invitations invitation
      JOIN tenants tenant
        ON tenant.tenant_id=invitation.tenant_id
      JOIN roles target_role
        ON target_role.tenant_id=invitation.tenant_id
       AND target_role.code=invitation.role_code
      JOIN users actor
        ON actor.tenant_id=invitation.tenant_id AND actor.user_id=NEW.actor_user_id
      WHERE invitation.tenant_id=NEW.tenant_id
        AND invitation.invitation_id=NEW.object_id
        AND tenant.status='active' AND target_role.status='active'
        AND actor.role=NEW.actor_role AND actor.status='active'
        AND (
          (
            NEW.action='user.invited'
            AND NEW.actor_email=actor.email
            AND invitation.status='pending'
            AND invitation.invited_by=NEW.actor_user_id
            AND invitation.created_at=NEW.created_at
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
            AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=5
            AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.after_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND json_extract(NEW.after_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(NEW.after_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(NEW.after_summary_json,'$.status')='pending'
            AND json_extract(NEW.after_summary_json,'$.expires_at')=invitation.expires_at
            AND json_type(NEW.after_summary_json,'$.accepted_user_id')='null'
            AND json(NEW.changed_fields_json)
              =json('["email_fingerprint","expires_at","role_code","status"]')
          )
          OR
          (
            NEW.action='invitation.accepted'
            AND NEW.actor_email='invitation-acceptor@redacted.invalid'
            AND invitation.status='accepted'
            AND invitation.accepted_user_id=NEW.actor_user_id
            AND actor.role=invitation.role_code
            AND invitation.email=actor.email
            AND invitation.accepted_at=NEW.created_at
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=5
            AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=5
            AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.before_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.after_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND json_extract(NEW.before_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(NEW.before_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(NEW.before_summary_json,'$.status')='pending'
            AND json_extract(NEW.before_summary_json,'$.expires_at')=invitation.expires_at
            AND json_type(NEW.before_summary_json,'$.accepted_user_id')='null'
            AND json_extract(NEW.after_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(NEW.after_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(NEW.after_summary_json,'$.status')='accepted'
            AND json_extract(NEW.after_summary_json,'$.expires_at')=invitation.expires_at
            AND json_extract(NEW.after_summary_json,'$.accepted_user_id')
              =invitation.accepted_user_id
            AND json(NEW.changed_fields_json)=json('["accepted_user_id","status"]')
          )
        )
    )
  )
)
OR (
  NEW.object_type='invitation'
  AND NEW.action NOT IN ('user.invited','invitation.accepted')
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

-- 数据修正后立即恢复全表不可变约束，应用运行期不存在可写窗口。
CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;

-- 与 0051 完全相同的冲突条件在 owner 之后创建，保持重复请求先返回不可变冲突。
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
    OR (
      NEW.action IN ('user.invited','invitation.accepted')
      AND current.action IN ('user.invited','invitation.accepted')
      AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;

CREATE TEMP TABLE prd060_privacy_up_final_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd060_privacy_up_final_guard
BEFORE INSERT ON prd060_privacy_up_final_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd060_privacy_up_copy_failed'); END;
-- 同时证明逐列保真、脱敏覆盖、运行期 trigger 完整和租户复合外键健康。
INSERT INTO prd060_privacy_up_final_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_privacy_audit_expected
      EXCEPT SELECT rowid,* FROM audit_logs
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT * FROM prd060_privacy_audit_expected
    ))
  + (SELECT COUNT(*) FROM audit_logs
      WHERE action='invitation.accepted'
        AND actor_email!='invitation-acceptor@redacted.invalid')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN (
        'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
        'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
        'trg_audit_logs_prd050_rowid_insert'
      ))=5 THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='trigger' AND name='trg_audit_logs_owner_insert'),
      'invitation-acceptor@redacted.invalid')>0 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check);
DROP TRIGGER trg_prd060_privacy_up_final_guard;
DROP TABLE prd060_privacy_up_final_guard;
DROP TABLE prd060_privacy_audit_expected;

COMMIT;
