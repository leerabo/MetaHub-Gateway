-- PRD-060 隐私修正回滚：只在没有任何邀请业务事实、后续迁移或未知依赖时，
-- 恢复 0051 的原始 owner trigger。脱敏是不可逆操作，本脚本绝不猜测或恢复个人邮箱。
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd060_privacy_down_guard (
  business_fact_count INTEGER NOT NULL,
  unknown_dependency_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd060_privacy_down_guard
BEFORE INSERT ON prd060_privacy_down_guard FOR EACH ROW
WHEN NEW.business_fact_count!=0 OR NEW.unknown_dependency_count!=0
BEGIN SELECT RAISE(ABORT,'prd060_privacy_down_blocked'); END;

-- 在修改 owner trigger 前统计所有直接或间接 PRD-060 事实；零行导出的筛选条件也属于证据。
INSERT INTO prd060_privacy_down_guard(business_fact_count,unknown_dependency_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0051' AND status='applied')=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0052' AND status='applied')=1 THEN 0 ELSE 1 END
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
    WHERE status='applied' AND version>'0052')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.sql IS NOT NULL
        AND (
          lower(object.sql) LIKE '%invitations%'
          OR lower(object.sql) LIKE '%invitation_events%'
          OR lower(object.sql) LIKE '%user.invited%'
          OR lower(object.sql) LIKE '%invitation.accepted%'
          OR lower(object.sql) LIKE '%invitation-acceptor@redacted.invalid%'
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
DROP TRIGGER trg_prd060_privacy_down_guard;
DROP TABLE prd060_privacy_down_guard;

-- conflict 必须在恢复后的 owner 之后重新创建，维持 0051 的重复事实错误优先级。
DROP TRIGGER trg_audit_logs_prd050_conflict_insert;
DROP TRIGGER trg_audit_logs_owner_insert;

-- 以下 owner 定义精确恢复 0051；接受审计再次要求 actor_email 等于 User.email。
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
      JOIN users actor
        ON actor.tenant_id=invitation.tenant_id AND actor.user_id=NEW.actor_user_id
      WHERE invitation.tenant_id=NEW.tenant_id
        AND invitation.invitation_id=NEW.object_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND (
          (
            NEW.action='user.invited'
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
            AND invitation.status='accepted'
            AND invitation.accepted_user_id=NEW.actor_user_id
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

CREATE TEMP TABLE prd060_privacy_down_final_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd060_privacy_down_final_guard
BEFORE INSERT ON prd060_privacy_down_final_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd060_privacy_down_schema_invalid'); END;
-- 提交前证明 0051 owner 语义已恢复、隐私标记逻辑已移除且外键仍完整。
INSERT INTO prd060_privacy_down_final_guard(invalid_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN (
        'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
        'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
        'trg_audit_logs_prd050_rowid_insert'
      ))=5 THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='trigger' AND name='trg_audit_logs_owner_insert'),
      'actor.email=NEW.actor_email')>0 THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='trigger' AND name='trg_audit_logs_owner_insert'),
      'invitation-acceptor@redacted.invalid')=0 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check);
DROP TRIGGER trg_prd060_privacy_down_final_guard;
DROP TABLE prd060_privacy_down_final_guard;

COMMIT;
