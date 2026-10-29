-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE consumers_rollback_guard(value INTEGER CHECK(value=0));
INSERT INTO consumers_rollback_guard SELECT
  (SELECT COUNT(*) FROM workspace_consumers)+(SELECT COUNT(*) FROM virtual_key_consumers)
  +(SELECT COUNT(*) FROM audit_logs WHERE action IN('consumer.created','consumer.updated','virtual_key.rotated'));
DROP TABLE consumers_rollback_guard;
ALTER TABLE virtual_keys DROP COLUMN credential_revision;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DELETE FROM tenant_deletion_trigger_manifest WHERE source_table IN('workspace_consumers','virtual_key_consumers');
DELETE FROM tenant_deletion_table_manifest WHERE source_table IN('workspace_consumers','virtual_key_consumers');
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TABLE audit_logs_consumer_rebuild (
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
      'audit.exported','role.permission_changed',
      'user.invited','invitation.accepted',
      'config.imported','config.exported',
      'prompt_template.created','prompt_template.updated',
      'prompt_template.version_published',
      'policy_template.applied','policy_template.rolled_back',
      'experiment.created',
      'session_risk.freeze_session','session_risk.freeze_virtual_key',
      'session_risk.dismiss','session_risk.release_session',
      'session_risk.release_virtual_key',
      'response_cache.policy_created','response_cache.policy_updated',
      'response_cache.invalidated','branding.updated','tenant.export_requested','tenant.exported','tenant.deletion_requested','tenant.deleted',
      'admin_assistant.queried',
      'plan.created','plan.updated','quota.adjustment_created',
      'billing_dispute.created','billing_dispute.review_started',
      'billing_dispute.approved','billing_dispute.rejected','billing_dispute.cancelled',
      'mcp.server_registered','mcp.server_refreshed',
      'mcp.server_credential_rotated','mcp.server_updated',
      'mcp.server_disabled','mcp.server_enabled',
      'mcp.server_operation_completed','mcp.tool_refreshed',
      'mcp.tool_policy_updated','mcp.tool_grants_updated',
      'mcp.network_policy_updated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider','provider_key','virtual_key','budget_rule','dlp_rule',
      'webhook_delivery','webhook_endpoint','webhook_dead_letter',
      'usage_export_job','policy','policy_snapshot','policy_explanation',
      'routing_policy','model_quality_score','cache_affinity',
      'finops_adjustment','cost_anomaly','provider_error_map','audit_export',
      'role_permission','invitation','config_transfer',
      'prompt_template','prompt_template_version',
      'policy_template_application','session_risk','experiment',
      'response_cache_policy','response_cache_invalidation','branding_config','tenant_export_job','deletion_request',
      'assistant_query','plan','quota_adjustment','billing_dispute',
      'mcp_server','mcp_server_operation','mcp_tool','mcp_network_policy'
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
INSERT INTO audit_logs_consumer_rebuild(rowid,"audit_log_id","tenant_id","actor_user_id","actor_email","actor_role","action","object_type","object_id","object_name","before_summary_json","after_summary_json","changed_fields_json","client_ip","user_agent","request_id","trace_id","created_at") SELECT rowid,"audit_log_id","tenant_id","actor_user_id","actor_email","actor_role","action","object_type","object_id","object_name","before_summary_json","after_summary_json","changed_fields_json","client_ip","user_agent","request_id","trace_id","created_at" FROM audit_logs;
DROP TABLE audit_logs;
ALTER TABLE audit_logs_consumer_rebuild RENAME TO audit_logs;
CREATE UNIQUE INDEX idx_audit_logs_admin_assistant_owner_prd077
  ON audit_logs(tenant_id,object_id)
  WHERE action='admin_assistant.queried';
CREATE UNIQUE INDEX idx_audit_logs_billing_dispute_transition_prd080
  ON audit_logs(tenant_id,action,object_id,request_id)
  WHERE action IN('billing_dispute.created','billing_dispute.review_started',
    'billing_dispute.approved','billing_dispute.rejected','billing_dispute.cancelled');
CREATE UNIQUE INDEX idx_audit_logs_config_transfer_request_unique
  ON audit_logs(tenant_id,actor_user_id,action,request_id)
  WHERE action IN ('config.imported','config.exported');
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_experiment_owner_unique
  ON audit_logs(tenant_id,object_id) WHERE action='experiment.created';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id,object_id)
  WHERE action='finops.adjustment_created';
CREATE UNIQUE INDEX idx_audit_logs_invitation_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action IN ('user.invited','invitation.accepted');
CREATE UNIQUE INDEX idx_audit_logs_mcp_network_policy_prd083
  ON audit_logs(tenant_id,object_id,request_id)
  WHERE action='mcp.network_policy_updated';
CREATE UNIQUE INDEX idx_audit_logs_mcp_operation_prd082
  ON audit_logs(tenant_id,object_id,request_id)
  WHERE object_type='mcp_server_operation';
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_plan_request_prd079
  ON audit_logs(tenant_id,action,object_id,request_id)
  WHERE action IN('plan.created','plan.updated');
CREATE UNIQUE INDEX idx_audit_logs_policy_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN ('policy_template.applied','policy_template.rolled_back');
CREATE UNIQUE INDEX idx_audit_logs_prompt_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN (
    'prompt_template.created','prompt_template.updated',
    'prompt_template.version_published'
  );
CREATE UNIQUE INDEX idx_audit_logs_quota_adjustment_prd079
  ON audit_logs(tenant_id,object_id)
  WHERE action='quota.adjustment_created';
CREATE UNIQUE INDEX idx_audit_logs_response_cache_owner_prd076
  ON audit_logs(tenant_id,action,object_id,request_id)
  WHERE action IN (
    'response_cache.policy_created','response_cache.policy_updated',
    'response_cache.invalidated'
  );
CREATE UNIQUE INDEX idx_audit_logs_role_permission_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='role.permission_changed';
CREATE UNIQUE INDEX idx_audit_logs_session_risk_action_unique_prd070
  ON audit_logs(tenant_id,object_id,request_id)
  WHERE action IN (
    'session_risk.freeze_session','session_risk.freeze_virtual_key',
    'session_risk.dismiss','session_risk.release_session',
    'session_risk.release_virtual_key'
  );
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id,action,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id,actor_user_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id,object_type,object_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id,request_id);
CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id,created_at DESC,audit_log_id DESC);
CREATE UNIQUE INDEX uq_audit_logs_owner_prd068
  ON audit_logs(tenant_id,audit_log_id);
CREATE TRIGGER trg_audit_logs_admin_assistant_owner_insert_prd077
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action='admin_assistant.queried'
  AND (
    NEW.object_type!='assistant_query' OR NEW.object_name IS NOT NULL
    OR NEW.before_summary_json!='{}'
    OR NOT EXISTS(
      SELECT 1
      FROM assistant_audits assistant_audit
      JOIN assistant_queries query
        ON query.tenant_id=assistant_audit.tenant_id
       AND query.assistant_query_id=assistant_audit.assistant_query_id
      JOIN users actor
        ON actor.tenant_id=assistant_audit.tenant_id
       AND actor.user_id=assistant_audit.actor_user_id
      WHERE assistant_audit.tenant_id=NEW.tenant_id
        AND assistant_audit.assistant_query_id=NEW.object_id
        AND assistant_audit.audit_log_id=NEW.audit_log_id
        AND assistant_audit.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND assistant_audit.operation_request_id=NEW.request_id
        AND assistant_audit.operation_trace_id=NEW.trace_id
        AND assistant_audit.created_at=NEW.created_at
        AND query.query_type=json_extract(NEW.after_summary_json,'$.query_type')
        AND query.outcome=json_extract(NEW.after_summary_json,'$.outcome')
        AND query.claim_count=json_extract(NEW.after_summary_json,'$.claim_count')
        AND query.source_count=json_extract(NEW.after_summary_json,'$.source_count')
        AND query.redaction_count
            =json_extract(NEW.after_summary_json,'$.redaction_count')
        AND query.execution_mode
            =json_extract(NEW.after_summary_json,'$.execution_mode')
        AND query.model_call_count
            =json_extract(NEW.after_summary_json,'$.model_call_count')
        AND query.model_cost_status
            =json_extract(NEW.after_summary_json,'$.model_cost_status')
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=8
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'query_type','outcome','claim_count','source_count',
            'redaction_count','execution_mode','model_call_count',
            'model_cost_status'
          )
        )
        AND json_type(NEW.after_summary_json,'$.query_type')='text'
        AND json_type(NEW.after_summary_json,'$.outcome')='text'
        AND json_type(NEW.after_summary_json,'$.claim_count')='integer'
        AND json_type(NEW.after_summary_json,'$.source_count')='integer'
        AND json_type(NEW.after_summary_json,'$.redaction_count')='integer'
        AND json_type(NEW.after_summary_json,'$.execution_mode')='text'
        AND json_type(NEW.after_summary_json,'$.model_call_count')='integer'
        AND json_type(NEW.after_summary_json,'$.model_cost_status')='text'
        AND json(NEW.changed_fields_json)=json(
          '["claim_count","execution_mode","model_call_count",' ||
          '"model_cost_status","outcome","query_type","redaction_count",' ||
          '"source_count"]'
        )
    )
  )
)
OR (
  NEW.object_type='assistant_query'
  AND NEW.action!='admin_assistant.queried'
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_billing_dispute_owner_insert_prd080
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN('billing_dispute.created','billing_dispute.review_started',
    'billing_dispute.approved','billing_dispute.rejected','billing_dispute.cancelled')
  AND (
    NEW.object_type!='billing_dispute' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS(
      SELECT 1 FROM billing_disputes dispute
      JOIN billing_dispute_transitions transition
        ON transition.tenant_id=dispute.tenant_id
       AND transition.billing_dispute_id=dispute.billing_dispute_id
      WHERE dispute.tenant_id=NEW.tenant_id AND dispute.billing_dispute_id=NEW.object_id
        AND transition.action=CASE NEW.action
          WHEN 'billing_dispute.created' THEN 'create'
          WHEN 'billing_dispute.review_started' THEN 'start_review'
          WHEN 'billing_dispute.approved' THEN 'approve'
          WHEN 'billing_dispute.rejected' THEN 'reject' ELSE 'cancel' END
        AND transition.actor_user_id=NEW.actor_user_id
        AND transition.operation_request_id=NEW.request_id
        AND transition.operation_trace_id=NEW.trace_id
        AND transition.to_status=dispute.status
        AND transition.new_revision=dispute.state_revision
        AND ((transition.action='create' AND NEW.before_summary_json='{}')
          OR (transition.action!='create'
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=7
            AND NOT EXISTS(SELECT 1 FROM json_each(NEW.before_summary_json) field
              WHERE field.key NOT IN('status','state_revision','scope_type','item_count',
                'adjustment_count','currency_totals_sha256','reason_sha256'))
            AND json_extract(NEW.before_summary_json,'$.status')=transition.from_status
            AND json_extract(NEW.before_summary_json,'$.state_revision')
              =transition.expected_revision
            AND json_extract(NEW.before_summary_json,'$.scope_type')=dispute.scope_type
            AND json_extract(NEW.before_summary_json,'$.item_count')=dispute.item_count
            AND json_extract(NEW.before_summary_json,'$.adjustment_count')=0
            AND json_extract(NEW.before_summary_json,'$.currency_totals_sha256')
              =metahub_sha256('{}')
            AND json_extract(NEW.before_summary_json,'$.reason_sha256')
              =dispute.reason_sha256))
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=7
        AND NOT EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN('status','state_revision','scope_type','item_count',
            'adjustment_count','currency_totals_sha256','reason_sha256'))
        AND json_extract(NEW.after_summary_json,'$.status')=dispute.status
        AND json_extract(NEW.after_summary_json,'$.state_revision')=dispute.state_revision
        AND json_extract(NEW.after_summary_json,'$.scope_type')=dispute.scope_type
        AND json_extract(NEW.after_summary_json,'$.item_count')=dispute.item_count
        AND json_extract(NEW.after_summary_json,'$.adjustment_count')
          =transition.approved_item_count
        AND json_extract(NEW.after_summary_json,'$.currency_totals_sha256')=
          CASE transition.action
            WHEN 'create' THEN (
              SELECT metahub_sha256(json_extract(event.payload_json,'$.currency_totals'))
              FROM billing_dispute_events event
              WHERE event.tenant_id=dispute.tenant_id
                AND event.billing_dispute_id=dispute.billing_dispute_id
                AND event.event_type='billing_dispute.created')
            WHEN 'approve' THEN transition.approved_totals_sha256
            ELSE metahub_sha256('{}')
          END
        AND json_extract(NEW.after_summary_json,'$.reason_sha256')=dispute.reason_sha256
        AND NEW.changed_fields_json=CASE transition.action
          WHEN 'create' THEN
            '["adjustment_count","currency_totals_sha256","item_count","reason_sha256","scope_type","state_revision","status"]'
          WHEN 'approve' THEN
            '["adjustment_count","currency_totals_sha256","state_revision","status"]'
          ELSE '["state_revision","status"]' END
        AND (SELECT COUNT(*) FROM billing_dispute_adjustment_links link
          WHERE link.tenant_id=dispute.tenant_id
            AND link.billing_dispute_id=dispute.billing_dispute_id
            AND link.billing_dispute_transition_id=transition.billing_dispute_transition_id)
          =transition.approved_item_count
        AND ((transition.action IN('approve','reject','cancel')
            AND NOT EXISTS(SELECT 1 FROM billing_settlement_holds hold
              WHERE hold.tenant_id=dispute.tenant_id
                AND hold.billing_dispute_id=dispute.billing_dispute_id
                AND hold.status='active'))
          OR (transition.action IN('create','start_review')
            AND (SELECT COUNT(*) FROM billing_settlement_holds hold
              WHERE hold.tenant_id=dispute.tenant_id
                AND hold.billing_dispute_id=dispute.billing_dispute_id
                AND hold.status='active')=dispute.item_count))
    )
  )
)
OR (NEW.object_type='billing_dispute' AND NEW.action NOT IN(
  'billing_dispute.created','billing_dispute.review_started','billing_dispute.approved',
  'billing_dispute.rejected','billing_dispute.cancelled'))
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_export_sequence_insert_prd058
AFTER INSERT ON audit_logs FOR EACH ROW
BEGIN
  -- 每条新审计事实都登记导出序列，配置迁移审计不得绕过既有增量导出链。
  INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id)
  VALUES(NEW.tenant_id,NEW.audit_log_id);
END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_mcp_network_policy_insert_prd083
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (NEW.action='mcp.network_policy_updated' AND (
  NEW.object_type!='mcp_network_policy' OR NEW.object_name IS NOT NULL
  OR (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))!=11
  OR (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))!=11
  OR (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))
    !=(SELECT COUNT(DISTINCT key) FROM json_each(NEW.before_summary_json))
  OR (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))
    !=(SELECT COUNT(DISTINCT key) FROM json_each(NEW.after_summary_json))
  OR (SELECT COUNT(*) FROM json_each(NEW.changed_fields_json))
    !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.changed_fields_json))
  OR EXISTS(SELECT 1 FROM json_each(NEW.before_summary_json) field
    WHERE field.key NOT IN(
      'mcp_server_id','status','revision','egress_mode','destination_count',
      'connect_timeout_seconds','probe_timeout_seconds','call_timeout_seconds',
      'failure_threshold','open_seconds','waiting_job_resume_count'))
  OR EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
    WHERE field.key NOT IN(
      'mcp_server_id','status','revision','egress_mode','destination_count',
      'connect_timeout_seconds','probe_timeout_seconds','call_timeout_seconds',
      'failure_threshold','open_seconds','waiting_job_resume_count'))
  OR EXISTS(SELECT 1 FROM json_each(NEW.changed_fields_json) field
    WHERE field.type!='text' OR field.value NOT IN(
      'mcp_server_id','status','revision','egress_mode','destination_count',
      'connect_timeout_seconds','probe_timeout_seconds','call_timeout_seconds',
      'failure_threshold','open_seconds','waiting_job_resume_count'))
  OR NOT EXISTS(
    SELECT 1
    FROM mcp_network_policies policy
    JOIN mcp_network_policy_revisions current
      ON current.tenant_id=policy.tenant_id
     AND current.mcp_network_policy_id=policy.mcp_network_policy_id
     AND current.policy_revision=policy.current_revision
    JOIN mcp_network_policy_revisions previous
      ON previous.tenant_id=current.tenant_id
     AND previous.mcp_network_policy_id=current.mcp_network_policy_id
     AND previous.policy_revision=current.policy_revision-1
    JOIN users actor ON actor.tenant_id=current.tenant_id
      AND actor.user_id=current.updated_by
    WHERE policy.tenant_id=NEW.tenant_id
      AND policy.mcp_network_policy_id=NEW.object_id
      AND actor.status='active' AND NEW.actor_user_id=actor.user_id
      AND NEW.actor_email=actor.email AND NEW.actor_role=actor.role
      AND NEW.request_id=current.request_id AND NEW.trace_id=current.trace_id
      AND NEW.created_at=current.created_at AND policy.updated_at=current.created_at
      AND json_extract(NEW.before_summary_json,'$.mcp_server_id')=previous.mcp_server_id
      AND json_extract(NEW.after_summary_json,'$.mcp_server_id')=current.mcp_server_id
      AND json_extract(NEW.before_summary_json,'$.status')=previous.status
      AND json_extract(NEW.after_summary_json,'$.status')=current.status
      AND json_extract(NEW.before_summary_json,'$.revision')=previous.policy_revision
      AND json_extract(NEW.after_summary_json,'$.revision')=current.policy_revision
      AND json_extract(NEW.before_summary_json,'$.egress_mode')=previous.egress_mode
      AND json_extract(NEW.after_summary_json,'$.egress_mode')=current.egress_mode
      AND json_extract(NEW.before_summary_json,'$.destination_count')
        =CASE previous.status WHEN 'active' THEN 1 ELSE 0 END
      AND json_extract(NEW.after_summary_json,'$.destination_count')
        =CASE current.status WHEN 'active' THEN 1 ELSE 0 END
      AND json_extract(NEW.before_summary_json,'$.connect_timeout_seconds')
        =previous.connect_timeout_seconds
      AND json_extract(NEW.after_summary_json,'$.connect_timeout_seconds')
        =current.connect_timeout_seconds
      AND json_extract(NEW.before_summary_json,'$.probe_timeout_seconds')
        =previous.probe_timeout_seconds
      AND json_extract(NEW.after_summary_json,'$.probe_timeout_seconds')
        =current.probe_timeout_seconds
      AND json_extract(NEW.before_summary_json,'$.call_timeout_seconds')
        =previous.call_timeout_seconds
      AND json_extract(NEW.after_summary_json,'$.call_timeout_seconds')
        =current.call_timeout_seconds
      AND json_extract(NEW.before_summary_json,'$.failure_threshold')
        =previous.failure_threshold
      AND json_extract(NEW.after_summary_json,'$.failure_threshold')
        =current.failure_threshold
      AND json_extract(NEW.before_summary_json,'$.open_seconds')=previous.open_seconds
      AND json_extract(NEW.after_summary_json,'$.open_seconds')=current.open_seconds
      AND json_extract(NEW.before_summary_json,'$.waiting_job_resume_count')=0
      AND json_extract(NEW.after_summary_json,'$.waiting_job_resume_count')=CASE
        WHEN previous.status='disabled' AND current.status='active'
          AND EXISTS(SELECT 1 FROM mcp_server_sync_jobs job
          WHERE job.tenant_id=current.tenant_id
            AND job.mcp_server_id=current.mcp_server_id
            AND job.status='pending' AND job.attempt_count=0
            AND job.next_attempt_at=current.created_at
            AND job.updated_at=current.created_at)
        THEN 1 ELSE 0 END
  )
  OR EXISTS(
    SELECT 1 FROM json_each(
      '["mcp_server_id","status","revision","egress_mode","destination_count",'
      ||'"connect_timeout_seconds","probe_timeout_seconds","call_timeout_seconds",'
      ||'"failure_threshold","open_seconds","waiting_job_resume_count"]'
    ) expected
    WHERE (json_extract(NEW.before_summary_json,'$.'||expected.value)
        IS NOT json_extract(NEW.after_summary_json,'$.'||expected.value))
      !=EXISTS(SELECT 1 FROM json_each(NEW.changed_fields_json) actual
        WHERE actual.value=expected.value)
  )
)) OR (NEW.object_type='mcp_network_policy'
  AND NEW.action!='mcp.network_policy_updated')
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_mcp_operation_insert_prd082
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN NEW.action='mcp.server_operation_completed' AND (
  (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))!=7
  OR (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))!=7
  OR NOT EXISTS(
    SELECT 1
    FROM mcp_server_operations operation
    JOIN users actor ON actor.tenant_id=operation.tenant_id
      AND actor.user_id=operation.actor_user_id
    WHERE operation.tenant_id=NEW.tenant_id
      AND operation.mcp_server_operation_id=NEW.object_id
      AND operation.status IN('succeeded','failed')
      AND (operation.status='failed' OR actor.status='active')
      AND NEW.actor_user_id=actor.user_id
      AND NEW.actor_email=actor.email
      AND NEW.actor_role=actor.role
      AND NEW.request_id=operation.request_id
      AND NEW.trace_id=operation.trace_id
      AND NEW.created_at=operation.completed_at
      AND json_type(NEW.before_summary_json,'$.mcp_server_operation_id')='text'
      AND json_extract(NEW.before_summary_json,'$.mcp_server_operation_id')
        =operation.mcp_server_operation_id
      AND json_type(NEW.before_summary_json,'$.mcp_server_id')='text'
      AND json_extract(NEW.before_summary_json,'$.mcp_server_id')=operation.mcp_server_id
      AND json_type(NEW.before_summary_json,'$.operation_type')='text'
      AND json_extract(NEW.before_summary_json,'$.operation_type')=operation.operation_type
      AND json_type(NEW.before_summary_json,'$.status')='text'
      AND json_extract(NEW.before_summary_json,'$.status')='processing'
      AND json_type(NEW.before_summary_json,'$.start_server_revision')='integer'
      AND json_extract(NEW.before_summary_json,'$.start_server_revision')
        =operation.start_server_revision
      AND json_type(NEW.before_summary_json,'$.candidate_credential_revision')
        IN('integer','null')
      AND json_extract(NEW.before_summary_json,'$.candidate_credential_revision')
        IS operation.candidate_credential_revision
      AND json_type(NEW.before_summary_json,'$.error_code')='null'
      AND json_type(NEW.after_summary_json,'$.mcp_server_operation_id')='text'
      AND json_extract(NEW.after_summary_json,'$.mcp_server_operation_id')
        =operation.mcp_server_operation_id
      AND json_type(NEW.after_summary_json,'$.mcp_server_id')='text'
      AND json_extract(NEW.after_summary_json,'$.mcp_server_id')=operation.mcp_server_id
      AND json_type(NEW.after_summary_json,'$.operation_type')='text'
      AND json_extract(NEW.after_summary_json,'$.operation_type')=operation.operation_type
      AND json_type(NEW.after_summary_json,'$.status')='text'
      AND json_extract(NEW.after_summary_json,'$.status')=operation.status
      AND json_type(NEW.after_summary_json,'$.start_server_revision')='integer'
      AND json_extract(NEW.after_summary_json,'$.start_server_revision')
        =operation.start_server_revision
      AND json_type(NEW.after_summary_json,'$.candidate_credential_revision')
        IN('integer','null')
      AND json_extract(NEW.after_summary_json,'$.candidate_credential_revision')
        IS operation.candidate_credential_revision
      AND json_type(NEW.after_summary_json,'$.error_code')
        =CASE WHEN operation.error_code IS NULL THEN 'null' ELSE 'text' END
      AND json_extract(NEW.after_summary_json,'$.error_code') IS operation.error_code
  )
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_mcp_owner_insert_prd082
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (NEW.action LIKE 'mcp.%' AND NEW.action!='mcp.network_policy_updated' AND (
    NEW.object_name IS NOT NULL
    OR (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))
      !=(SELECT COUNT(DISTINCT key) FROM json_each(NEW.before_summary_json))
    OR (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))
      !=(SELECT COUNT(DISTINCT key) FROM json_each(NEW.after_summary_json))
    OR (SELECT COUNT(*) FROM json_each(NEW.changed_fields_json))
      !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.changed_fields_json))
    OR EXISTS(SELECT 1 FROM json_each(NEW.before_summary_json) field
      WHERE (NEW.object_type='mcp_server' AND field.key NOT IN(
          'mcp_server_id','status','state_revision','active_credential_revision',
          'tool_count','active_tool_count'))
        OR (NEW.object_type='mcp_server_operation' AND field.key NOT IN(
          'mcp_server_operation_id','mcp_server_id','operation_type','status',
          'start_server_revision','candidate_credential_revision','error_code'))
        OR (NEW.object_type='mcp_tool' AND field.key NOT IN(
          'mcp_tool_id','mcp_server_id','status','schema_revision','policy_revision',
          'risk_level','approval_required','required_permission_count',
          'role_grant_count','user_grant_count','redaction_count')))
    OR EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
      WHERE (NEW.object_type='mcp_server' AND field.key NOT IN(
          'mcp_server_id','status','state_revision','active_credential_revision',
          'tool_count','active_tool_count'))
        OR (NEW.object_type='mcp_server_operation' AND field.key NOT IN(
          'mcp_server_operation_id','mcp_server_id','operation_type','status',
          'start_server_revision','candidate_credential_revision','error_code'))
        OR (NEW.object_type='mcp_tool' AND field.key NOT IN(
          'mcp_tool_id','mcp_server_id','status','schema_revision','policy_revision',
          'risk_level','approval_required','required_permission_count',
          'role_grant_count','user_grant_count','redaction_count')))
    OR EXISTS(SELECT 1 FROM json_each(NEW.changed_fields_json) field
      WHERE field.type!='text' OR
        (NEW.object_type='mcp_server' AND field.value NOT IN(
          'mcp_server_id','status','state_revision','active_credential_revision',
          'tool_count','active_tool_count')) OR
        (NEW.object_type='mcp_server_operation' AND field.value NOT IN(
          'mcp_server_operation_id','mcp_server_id','operation_type','status',
          'start_server_revision','candidate_credential_revision','error_code')) OR
        (NEW.object_type='mcp_tool' AND field.value NOT IN(
          'mcp_tool_id','mcp_server_id','status','schema_revision','policy_revision',
          'risk_level','approval_required','required_permission_count',
          'role_grant_count','user_grant_count','redaction_count')))
    OR EXISTS(
      SELECT 1 FROM json_each(CASE NEW.object_type
        WHEN 'mcp_server' THEN
          '["mcp_server_id","status","state_revision","active_credential_revision",' ||
          '"tool_count","active_tool_count"]'
        WHEN 'mcp_server_operation' THEN
          '["mcp_server_operation_id","mcp_server_id","operation_type","status",' ||
          '"start_server_revision","candidate_credential_revision","error_code"]'
        WHEN 'mcp_tool' THEN
          '["mcp_tool_id","mcp_server_id","status","schema_revision",' ||
          '"policy_revision","risk_level","approval_required",' ||
          '"required_permission_count","role_grant_count","user_grant_count",' ||
          '"redaction_count"]'
        ELSE '[]' END) expected
      WHERE (json_extract(NEW.before_summary_json,'$.'||expected.value)
          IS NOT json_extract(NEW.after_summary_json,'$.'||expected.value))
        !=EXISTS(SELECT 1 FROM json_each(NEW.changed_fields_json) actual
          WHERE actual.value=expected.value)
    )
    OR (NEW.object_type='mcp_server' AND NOT EXISTS(
      SELECT 1 FROM mcp_servers server WHERE server.tenant_id=NEW.tenant_id
        AND server.mcp_server_id=NEW.object_id))
    OR (NEW.object_type='mcp_server_operation' AND NOT EXISTS(
      SELECT 1 FROM mcp_server_operations operation WHERE operation.tenant_id=NEW.tenant_id
        AND operation.mcp_server_operation_id=NEW.object_id))
    OR (NEW.object_type='mcp_tool' AND NOT EXISTS(
      SELECT 1 FROM mcp_tools tool WHERE tool.tenant_id=NEW.tenant_id
        AND tool.mcp_tool_id=NEW.object_id))
    OR (NEW.action IN('mcp.server_registered','mcp.server_refreshed',
        'mcp.server_credential_rotated','mcp.server_updated',
        'mcp.server_disabled','mcp.server_enabled')
      AND NEW.object_type!='mcp_server')
    OR (NEW.action='mcp.server_operation_completed'
      AND NEW.object_type!='mcp_server_operation')
    OR (NEW.action IN('mcp.tool_refreshed','mcp.tool_policy_updated',
        'mcp.tool_grants_updated') AND NEW.object_type!='mcp_tool')
  ))
  OR (NEW.object_type IN('mcp_server','mcp_server_operation','mcp_tool')
    AND NEW.action NOT LIKE 'mcp.%')
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_mcp_server_insert_prd082
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN NEW.action IN(
  'mcp.server_registered','mcp.server_refreshed','mcp.server_credential_rotated',
  'mcp.server_updated','mcp.server_disabled','mcp.server_enabled'
) AND (
  (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))!=6
  OR (NEW.action='mcp.server_registered'
    AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))!=0)
  OR (NEW.action!='mcp.server_registered'
    AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))!=6)
  OR NOT EXISTS(
    SELECT 1
    FROM mcp_servers server
    JOIN mcp_server_operations operation
      ON operation.tenant_id=server.tenant_id
     AND operation.mcp_server_id=server.mcp_server_id
    JOIN users actor ON actor.tenant_id=operation.tenant_id
      AND actor.user_id=operation.actor_user_id
    WHERE server.tenant_id=NEW.tenant_id
      AND server.mcp_server_id=NEW.object_id
      AND actor.status='active'
      AND NEW.actor_user_id=actor.user_id
      AND NEW.actor_email=actor.email
      AND NEW.actor_role=actor.role
      AND server.updated_by=operation.actor_user_id
      AND NEW.request_id=operation.request_id
      AND NEW.trace_id=operation.trace_id
      AND operation.operation_type=CASE NEW.action
        WHEN 'mcp.server_registered' THEN 'register'
        WHEN 'mcp.server_refreshed' THEN 'refresh'
        WHEN 'mcp.server_credential_rotated' THEN 'rotate'
        WHEN 'mcp.server_updated' THEN 'update'
        WHEN 'mcp.server_disabled' THEN 'disable'
        WHEN 'mcp.server_enabled' THEN 'enable' END
      AND json_type(NEW.after_summary_json,'$.mcp_server_id')='text'
      AND json_extract(NEW.after_summary_json,'$.mcp_server_id')=server.mcp_server_id
      AND json_type(NEW.after_summary_json,'$.status')='text'
      AND json_extract(NEW.after_summary_json,'$.status')=server.status
      AND json_type(NEW.after_summary_json,'$.state_revision')='integer'
      AND json_extract(NEW.after_summary_json,'$.state_revision')=server.state_revision
      AND json_type(NEW.after_summary_json,'$.active_credential_revision')
        IN('integer','null')
      AND json_extract(NEW.after_summary_json,'$.active_credential_revision')
        IS server.active_credential_revision
      AND json_type(NEW.after_summary_json,'$.tool_count')='integer'
      AND json_extract(NEW.after_summary_json,'$.tool_count')=(
        SELECT COUNT(*) FROM mcp_tools tool
        WHERE tool.tenant_id=server.tenant_id
          AND tool.mcp_server_id=server.mcp_server_id)
      AND json_type(NEW.after_summary_json,'$.active_tool_count')='integer'
      AND json_extract(NEW.after_summary_json,'$.active_tool_count')=(
        SELECT COUNT(*) FROM mcp_tools tool
        WHERE tool.tenant_id=server.tenant_id
          AND tool.mcp_server_id=server.mcp_server_id AND tool.status='active')
      AND (
        (NEW.action='mcp.server_registered'
          AND operation.status='queued'
          AND NEW.created_at=operation.created_at
          AND server.created_at=operation.created_at
          AND server.updated_at=operation.created_at
          AND server.created_by=operation.actor_user_id
          AND operation.start_server_revision=1
          AND server.state_revision=1
          AND server.status='pending_sync'
          AND server.active_credential_revision IS NULL
          AND operation.candidate_credential_revision=1)
        OR
        (NEW.action!='mcp.server_registered'
          AND operation.status='succeeded'
          AND NEW.created_at=operation.completed_at
          AND server.updated_at=operation.completed_at
          AND json_type(NEW.before_summary_json,'$.mcp_server_id')='text'
          AND json_extract(NEW.before_summary_json,'$.mcp_server_id')=server.mcp_server_id
          AND json_type(NEW.before_summary_json,'$.status')='text'
          AND json_extract(NEW.before_summary_json,'$.status')
            IN('pending_sync','active','sync_failed','disabled')
          AND json_type(NEW.before_summary_json,'$.state_revision')='integer'
          AND json_extract(NEW.before_summary_json,'$.state_revision')
            =operation.start_server_revision
          AND server.state_revision=operation.start_server_revision+1
          AND json_type(NEW.before_summary_json,'$.active_credential_revision')
            IN('integer','null')
          AND json_extract(NEW.before_summary_json,'$.active_credential_revision')
            IS operation.start_credential_revision
          AND json_type(NEW.before_summary_json,'$.tool_count')='integer'
          AND json_extract(NEW.before_summary_json,'$.tool_count')>=0
          AND json_type(NEW.before_summary_json,'$.active_tool_count')='integer'
          AND json_extract(NEW.before_summary_json,'$.active_tool_count')>=0
          AND (NEW.action!='mcp.server_disabled' OR server.status='disabled')
          AND (NEW.action!='mcp.server_enabled' OR server.status='active')
          AND (NEW.action NOT IN('mcp.server_refreshed','mcp.server_credential_rotated')
            OR server.status='active')
          AND (NEW.action!='mcp.server_credential_rotated'
            OR server.active_credential_revision=operation.candidate_credential_revision)
          AND (NEW.action='mcp.server_credential_rotated'
            OR operation.candidate_credential_revision IS NULL)
          AND (NEW.action IN('mcp.server_credential_rotated','mcp.server_registered')
            OR server.active_credential_revision IS operation.start_credential_revision))
      )
  )
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_mcp_tool_insert_prd082
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN NEW.action IN(
  'mcp.tool_refreshed','mcp.tool_policy_updated','mcp.tool_grants_updated'
) AND (
  (NEW.action='mcp.tool_refreshed' AND (
    (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))!=11
    OR (SELECT COUNT(*) FROM json_each(NEW.before_summary_json)) NOT IN(0,11)))
  OR (NEW.action='mcp.tool_policy_updated' AND (
    (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))!=9
    OR (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))!=9))
  OR (NEW.action='mcp.tool_grants_updated' AND (
    (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))!=5
    OR (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))!=5))
  OR NOT EXISTS(
    SELECT 1
    FROM mcp_tools tool
    JOIN users actor ON actor.tenant_id=tool.tenant_id
      AND actor.user_id=tool.updated_by
    WHERE tool.tenant_id=NEW.tenant_id
      AND tool.mcp_tool_id=NEW.object_id
      AND actor.status='active'
      AND NEW.actor_user_id=actor.user_id
      AND NEW.actor_email=actor.email
      AND NEW.actor_role=actor.role
      AND NEW.created_at=tool.updated_at
      AND json_type(NEW.after_summary_json,'$.mcp_tool_id')='text'
      AND json_extract(NEW.after_summary_json,'$.mcp_tool_id')=tool.mcp_tool_id
      AND json_type(NEW.after_summary_json,'$.mcp_server_id')='text'
      AND json_extract(NEW.after_summary_json,'$.mcp_server_id')=tool.mcp_server_id
      AND json_type(NEW.after_summary_json,'$.policy_revision')='integer'
      AND json_extract(NEW.after_summary_json,'$.policy_revision')=tool.policy_revision
      AND (
        (NEW.action='mcp.tool_refreshed'
          AND json_type(NEW.after_summary_json,'$.status')='text'
          AND json_extract(NEW.after_summary_json,'$.status')=tool.status
          AND json_type(NEW.after_summary_json,'$.schema_revision')='integer'
          AND json_extract(NEW.after_summary_json,'$.schema_revision')=tool.schema_revision
          AND json_type(NEW.after_summary_json,'$.risk_level')='text'
          AND json_extract(NEW.after_summary_json,'$.risk_level')=tool.risk_level
          AND json_type(NEW.after_summary_json,'$.approval_required') IN('true','false')
          AND json_extract(NEW.after_summary_json,'$.approval_required')
            =tool.approval_required
          AND json_type(NEW.after_summary_json,'$.required_permission_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.required_permission_count')
            =json_array_length(tool.required_permission_codes_json)
          AND json_type(NEW.after_summary_json,'$.role_grant_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.role_grant_count')=(
            SELECT COUNT(*) FROM mcp_tool_role_grants role_grant
            WHERE role_grant.tenant_id=tool.tenant_id
              AND role_grant.mcp_tool_id=tool.mcp_tool_id)
          AND json_type(NEW.after_summary_json,'$.user_grant_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.user_grant_count')=(
            SELECT COUNT(*) FROM mcp_tool_user_grants user_grant
            WHERE user_grant.tenant_id=tool.tenant_id
              AND user_grant.mcp_tool_id=tool.mcp_tool_id)
          AND json_type(NEW.after_summary_json,'$.redaction_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.redaction_count')
            =json_array_length(tool.result_redaction_pointers_json)
          AND EXISTS(
            SELECT 1 FROM mcp_server_operations operation
            WHERE operation.tenant_id=tool.tenant_id
              AND operation.mcp_server_id=tool.mcp_server_id
              AND operation.operation_type IN('register','refresh')
              AND operation.status='succeeded'
              AND operation.actor_user_id=tool.updated_by
              AND operation.request_id=NEW.request_id
              AND operation.trace_id=NEW.trace_id
              AND operation.completed_at=NEW.created_at
              AND (
                ((SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
                  AND tool.created_at=tool.updated_at
                  AND tool.schema_revision=1 AND tool.policy_revision=1)
                OR
                ((SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=11
                  AND operation.operation_type='refresh'
                  AND json_extract(NEW.before_summary_json,'$.mcp_tool_id')=tool.mcp_tool_id
                  AND json_extract(NEW.before_summary_json,'$.mcp_server_id')
                    =tool.mcp_server_id
                  AND json_type(NEW.before_summary_json,'$.status')='text'
                  AND json_extract(NEW.before_summary_json,'$.status')
                    IN('review_required','active','disabled')
                  AND json_type(NEW.before_summary_json,'$.schema_revision')='integer'
                  AND json_type(NEW.before_summary_json,'$.policy_revision')='integer'
                  AND json_type(NEW.before_summary_json,'$.risk_level')='text'
                  AND json_extract(NEW.before_summary_json,'$.risk_level')
                    IN('standard','high')
                  AND json_type(NEW.before_summary_json,'$.approval_required')
                    IN('true','false')
                  AND json_type(NEW.before_summary_json,'$.required_permission_count')
                    ='integer'
                  AND json_extract(NEW.before_summary_json,'$.required_permission_count')>=0
                  AND json_type(NEW.before_summary_json,'$.role_grant_count')='integer'
                  AND json_extract(NEW.before_summary_json,'$.role_grant_count')>=0
                  AND json_type(NEW.before_summary_json,'$.user_grant_count')='integer'
                  AND json_extract(NEW.before_summary_json,'$.user_grant_count')>=0
                  AND json_type(NEW.before_summary_json,'$.redaction_count')='integer'
                  AND json_extract(NEW.before_summary_json,'$.redaction_count')>=0
                  AND ((tool.schema_revision
                        =json_extract(NEW.before_summary_json,'$.schema_revision')+1
                      AND tool.policy_revision
                        =json_extract(NEW.before_summary_json,'$.policy_revision')+1
                      AND tool.status='review_required')
                    OR (tool.schema_revision
                        =json_extract(NEW.before_summary_json,'$.schema_revision')
                      AND tool.policy_revision
                        =json_extract(NEW.before_summary_json,'$.policy_revision')
                      AND tool.status='disabled')))))
        )
        OR
        (NEW.action='mcp.tool_policy_updated'
          AND json_type(NEW.before_summary_json,'$.mcp_tool_id')='text'
          AND json_extract(NEW.before_summary_json,'$.mcp_tool_id')=tool.mcp_tool_id
          AND json_type(NEW.before_summary_json,'$.mcp_server_id')='text'
          AND json_extract(NEW.before_summary_json,'$.mcp_server_id')=tool.mcp_server_id
          AND json_type(NEW.before_summary_json,'$.policy_revision')='integer'
          AND json_extract(NEW.before_summary_json,'$.policy_revision')+1
            =tool.policy_revision
          AND json_type(NEW.before_summary_json,'$.schema_revision')='integer'
          AND json_extract(NEW.before_summary_json,'$.schema_revision')
            =tool.schema_revision
          AND json_type(NEW.before_summary_json,'$.status')='text'
          AND json_extract(NEW.before_summary_json,'$.status')
            IN('review_required','active','disabled')
          AND json_type(NEW.before_summary_json,'$.risk_level')='text'
          AND json_extract(NEW.before_summary_json,'$.risk_level') IN('standard','high')
          AND json_type(NEW.before_summary_json,'$.approval_required') IN('true','false')
          AND json_type(NEW.before_summary_json,'$.required_permission_count')='integer'
          AND json_extract(NEW.before_summary_json,'$.required_permission_count')>=0
          AND json_type(NEW.before_summary_json,'$.redaction_count')='integer'
          AND json_extract(NEW.before_summary_json,'$.redaction_count')>=0
          AND json_type(NEW.after_summary_json,'$.status')='text'
          AND json_extract(NEW.after_summary_json,'$.status')=tool.status
          AND json_type(NEW.after_summary_json,'$.schema_revision')='integer'
          AND json_extract(NEW.after_summary_json,'$.schema_revision')=tool.schema_revision
          AND json_type(NEW.after_summary_json,'$.risk_level')='text'
          AND json_extract(NEW.after_summary_json,'$.risk_level')=tool.risk_level
          AND json_type(NEW.after_summary_json,'$.approval_required') IN('true','false')
          AND json_extract(NEW.after_summary_json,'$.approval_required')
            =tool.approval_required
          AND json_type(NEW.after_summary_json,'$.required_permission_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.required_permission_count')
            =json_array_length(tool.required_permission_codes_json)
          AND json_type(NEW.after_summary_json,'$.redaction_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.redaction_count')
            =json_array_length(tool.result_redaction_pointers_json))
        OR
        (NEW.action='mcp.tool_grants_updated'
          AND json_type(NEW.before_summary_json,'$.mcp_tool_id')='text'
          AND json_extract(NEW.before_summary_json,'$.mcp_tool_id')=tool.mcp_tool_id
          AND json_type(NEW.before_summary_json,'$.mcp_server_id')='text'
          AND json_extract(NEW.before_summary_json,'$.mcp_server_id')=tool.mcp_server_id
          AND json_type(NEW.before_summary_json,'$.policy_revision')='integer'
          AND json_extract(NEW.before_summary_json,'$.policy_revision')+1
            =tool.policy_revision
          AND json_type(NEW.before_summary_json,'$.role_grant_count')='integer'
          AND json_extract(NEW.before_summary_json,'$.role_grant_count')>=0
          AND json_type(NEW.before_summary_json,'$.user_grant_count')='integer'
          AND json_extract(NEW.before_summary_json,'$.user_grant_count')>=0
          AND json_type(NEW.after_summary_json,'$.role_grant_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.role_grant_count')=(
            SELECT COUNT(*) FROM mcp_tool_role_grants role_grant
            WHERE role_grant.tenant_id=tool.tenant_id
              AND role_grant.mcp_tool_id=tool.mcp_tool_id)
          AND json_type(NEW.after_summary_json,'$.user_grant_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.user_grant_count')=(
            SELECT COUNT(*) FROM mcp_tool_user_grants user_grant
            WHERE user_grant.tenant_id=tool.tenant_id
              AND user_grant.mcp_tool_id=tool.mcp_tool_id))
      )
  )
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
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
  NEW.action='config.imported'
  AND (
    NEW.object_type!='config_transfer' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM config_imports import_fact
      JOIN users actor
        ON actor.tenant_id=import_fact.tenant_id
       AND actor.user_id=import_fact.actor_user_id
      WHERE import_fact.tenant_id=NEW.tenant_id
        AND import_fact.config_import_id=NEW.object_id
        AND import_fact.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND import_fact.request_id=NEW.request_id
        AND import_fact.trace_id=NEW.trace_id
        AND import_fact.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=12
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'schema_version','manifest_sha256',
            'providers_created_count','providers_unchanged_count',
            'models_created_count','models_unchanged_count',
            'aliases_created_count','aliases_unchanged_count',
            'virtual_keys_created_count','virtual_keys_unchanged_count',
            'generated_virtual_key_count','disabled_virtual_key_count'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.schema_version')=import_fact.schema_version
        AND json_extract(NEW.after_summary_json,'$.manifest_sha256')=import_fact.manifest_sha256
        AND json_extract(NEW.after_summary_json,'$.providers_created_count')
          =import_fact.providers_created_count
        AND json_extract(NEW.after_summary_json,'$.providers_unchanged_count')
          =import_fact.providers_unchanged_count
        AND json_extract(NEW.after_summary_json,'$.models_created_count')
          =import_fact.models_created_count
        AND json_extract(NEW.after_summary_json,'$.models_unchanged_count')
          =import_fact.models_unchanged_count
        AND json_extract(NEW.after_summary_json,'$.aliases_created_count')
          =import_fact.aliases_created_count
        AND json_extract(NEW.after_summary_json,'$.aliases_unchanged_count')
          =import_fact.aliases_unchanged_count
        AND json_extract(NEW.after_summary_json,'$.virtual_keys_created_count')
          =import_fact.virtual_keys_created_count
        AND json_extract(NEW.after_summary_json,'$.virtual_keys_unchanged_count')
          =import_fact.virtual_keys_unchanged_count
        AND json_extract(NEW.after_summary_json,'$.generated_virtual_key_count')
          =import_fact.generated_virtual_key_count
        AND json_extract(NEW.after_summary_json,'$.disabled_virtual_key_count')
          =import_fact.disabled_virtual_key_count
        AND json(NEW.changed_fields_json)=json(
          '["aliases_created_count","aliases_unchanged_count",' ||
          '"disabled_virtual_key_count","generated_virtual_key_count",' ||
          '"manifest_sha256","models_created_count","models_unchanged_count",' ||
          '"providers_created_count","providers_unchanged_count","schema_version",' ||
          '"virtual_keys_created_count","virtual_keys_unchanged_count"]'
        )
        AND (SELECT COUNT(*) FROM config_import_rows row_fact
             WHERE row_fact.tenant_id=import_fact.tenant_id
               AND row_fact.config_import_id=import_fact.config_import_id)
          =import_fact.providers_created_count+import_fact.providers_unchanged_count
           +import_fact.models_created_count+import_fact.models_unchanged_count
           +import_fact.aliases_created_count+import_fact.aliases_unchanged_count
           +import_fact.virtual_keys_created_count+import_fact.virtual_keys_unchanged_count
    )
  )
)
OR (
  NEW.action='config.exported'
  AND (
    NEW.object_type!='config_transfer' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM config_exports export_fact
      JOIN users actor
        ON actor.tenant_id=export_fact.tenant_id
       AND actor.user_id=export_fact.actor_user_id
      WHERE export_fact.tenant_id=NEW.tenant_id
        AND export_fact.config_export_id=NEW.object_id
        AND export_fact.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND export_fact.request_id=NEW.request_id
        AND export_fact.trace_id=NEW.trace_id
        AND export_fact.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=6
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'schema_version','manifest_sha256','providers_count','models_count',
            'aliases_count','virtual_keys_count'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.schema_version')=export_fact.schema_version
        AND json_extract(NEW.after_summary_json,'$.manifest_sha256')=export_fact.manifest_sha256
        AND json_extract(NEW.after_summary_json,'$.providers_count')=export_fact.providers_count
        AND json_extract(NEW.after_summary_json,'$.models_count')=export_fact.models_count
        AND json_extract(NEW.after_summary_json,'$.aliases_count')=export_fact.aliases_count
        AND json_extract(NEW.after_summary_json,'$.virtual_keys_count')
          =export_fact.virtual_keys_count

        AND json(NEW.changed_fields_json)=json(
          '["aliases_count","manifest_sha256","models_count","providers_count",' ||
          '"schema_version","virtual_keys_count"]'
        )
    )
  )
)
OR (
  NEW.object_type='config_transfer'
  AND NEW.action NOT IN ('config.imported','config.exported')
)
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
-- Template create/update 必须绑定当前同租户 head、actor 和安全摘要白名单。
OR (
  NEW.action IN ('prompt_template.created','prompt_template.updated')
  AND (
    NEW.object_type!='prompt_template'
    OR NOT EXISTS (
      SELECT 1
      FROM prompt_templates template
      JOIN prompt_template_versions version
        ON version.tenant_id=template.tenant_id
       AND version.prompt_template_id=template.prompt_template_id
       AND version.prompt_template_version_id=template.current_version_id
       AND version.state='published'
      JOIN users actor
        ON actor.tenant_id=template.tenant_id AND actor.user_id=NEW.actor_user_id
      WHERE template.tenant_id=NEW.tenant_id
        AND template.prompt_template_id=NEW.object_id
        AND template.updated_by=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active' AND template.name=NEW.object_name
        AND json_extract(NEW.after_summary_json,'$.status')=template.status
        AND json_extract(NEW.after_summary_json,'$.revision')=template.revision
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.value NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND (
          NEW.action!='prompt_template.created'
          OR (
            (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
            AND json_extract(NEW.after_summary_json,'$.version_number')
              =version.version_number
            AND json_extract(NEW.after_summary_json,'$.project_scope')
              =version.project_scope
            AND json_extract(NEW.after_summary_json,'$.project_count')=(
              SELECT COUNT(*) FROM prompt_template_version_projects binding
              WHERE binding.tenant_id=version.tenant_id
                AND binding.prompt_template_version_id=version.prompt_template_version_id
            )
            AND json_extract(NEW.after_summary_json,'$.recommended_model_count')
              =version.recommended_model_count
            AND json_extract(NEW.after_summary_json,'$.definition_size_bytes')
              =version.definition_size_bytes
            AND json_extract(NEW.after_summary_json,'$.variable_count')
              =version.variable_count
            AND json_extract(NEW.after_summary_json,'$.change_summary_hmac')
              =version.change_summary_hmac
          )
        )
        AND (
          NEW.action!='prompt_template.updated'
          OR json_extract(NEW.before_summary_json,'$.revision')=template.revision-1
        )
    )
  )
)
-- Version publication Audit 绑定已封印版本、当前 head、publisher 和全部安全计数。
OR (
  NEW.action='prompt_template.version_published'
  AND (
    NEW.object_type!='prompt_template_version'
    OR NOT EXISTS (
      SELECT 1
      FROM prompt_template_versions version
      JOIN prompt_templates template
        ON template.tenant_id=version.tenant_id
       AND template.prompt_template_id=version.prompt_template_id
       AND template.current_version_id=version.prompt_template_version_id
      JOIN users actor
        ON actor.tenant_id=version.tenant_id AND actor.user_id=NEW.actor_user_id
      WHERE version.tenant_id=NEW.tenant_id
        AND version.prompt_template_version_id=NEW.object_id
        AND version.state='published' AND version.created_by=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active' AND template.name=NEW.object_name
        AND NEW.created_at=version.published_at
        AND json_extract(NEW.after_summary_json,'$.status')=template.status
        AND json_extract(NEW.after_summary_json,'$.revision')=template.revision
        AND json_extract(NEW.after_summary_json,'$.version_number')=version.version_number
        AND json_extract(NEW.after_summary_json,'$.project_scope')=version.project_scope
        AND json_extract(NEW.after_summary_json,'$.project_count')=(
          SELECT COUNT(*) FROM prompt_template_version_projects binding
          WHERE binding.tenant_id=version.tenant_id
            AND binding.prompt_template_version_id=version.prompt_template_version_id
        )
        AND json_extract(NEW.after_summary_json,'$.recommended_model_count')
          =version.recommended_model_count
        AND json_extract(NEW.after_summary_json,'$.definition_size_bytes')
          =version.definition_size_bytes
        AND json_extract(NEW.after_summary_json,'$.variable_count')=version.variable_count
        AND json_extract(NEW.after_summary_json,'$.change_summary_hmac')
          =version.change_summary_hmac
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.value NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
    )
  )
)
OR (
  NEW.object_type='prompt_template'
  AND NEW.action NOT IN ('prompt_template.created','prompt_template.updated')
)
OR (
  NEW.object_type='prompt_template_version'
  AND NEW.action!='prompt_template.version_published'
)
OR (
  NEW.action='experiment.created'
  AND (
    NEW.object_type!='experiment' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM model_experiments experiment
      JOIN users actor
        ON actor.tenant_id=experiment.tenant_id
       AND actor.user_id=experiment.created_by
      WHERE experiment.tenant_id=NEW.tenant_id
        AND experiment.experiment_id=NEW.object_id
        AND experiment.created_by=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND experiment.operation_request_id=NEW.request_id
        AND experiment.operation_trace_id=NEW.trace_id
        AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=12
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'experiment_id','project_id','environment_id','model_alias_id',
            'model_a_id','model_b_id','target_sample_count','traffic_sample_bps',
            'model_a_allocation_bps','sampling_duration_seconds',
            'feedback_collection_enabled','feedback_window_seconds'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.experiment_id')
          =experiment.experiment_id
        AND json_extract(NEW.after_summary_json,'$.project_id')=experiment.project_id
        AND json_extract(NEW.after_summary_json,'$.environment_id')
          =experiment.environment_id
        AND json_extract(NEW.after_summary_json,'$.model_alias_id')
          =experiment.model_alias_id
        AND json_extract(NEW.after_summary_json,'$.model_a_id')=experiment.model_a_id
        AND json_extract(NEW.after_summary_json,'$.model_b_id')=experiment.model_b_id
        AND json_extract(NEW.after_summary_json,'$.target_sample_count')
          =experiment.target_sample_count
        AND json_extract(NEW.after_summary_json,'$.traffic_sample_bps')
          =experiment.traffic_sample_bps
        AND json_extract(NEW.after_summary_json,'$.model_a_allocation_bps')
          =experiment.model_a_allocation_bps
        AND json_extract(NEW.after_summary_json,'$.sampling_duration_seconds')
          =experiment.sampling_duration_seconds
        AND json_extract(NEW.after_summary_json,'$.feedback_collection_enabled')
          =experiment.feedback_collection_enabled
        AND json_extract(NEW.after_summary_json,'$.feedback_window_seconds')
          =experiment.feedback_window_seconds
        AND json(NEW.changed_fields_json)=json(
          '["environment_id","experiment_id","feedback_collection_enabled",' ||
          '"feedback_window_seconds","model_a_allocation_bps","model_a_id",' ||
          '"model_alias_id","model_b_id","project_id","sampling_duration_seconds",' ||
          '"target_sample_count","traffic_sample_bps"]'
        )
    )
  )
)
OR (NEW.object_type='experiment' AND NEW.action!='experiment.created')
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_plan_billing_owner_insert_prd079
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN('plan.created','plan.updated')
  AND (
    NEW.object_type!='plan'
    OR NOT EXISTS(
      SELECT 1 FROM plans plan
      WHERE plan.tenant_id=NEW.tenant_id AND plan.plan_id=NEW.object_id
        AND plan.display_name=NEW.object_name
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=9
        AND NOT EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'display_name','base_tier','status','trial_status','trial_started_at',
            'trial_ends_at','trial_cycle','state_revision','quota_generation'))
        AND json_extract(NEW.after_summary_json,'$.display_name')=plan.display_name
        AND json_extract(NEW.after_summary_json,'$.base_tier')=plan.base_tier
        AND json_extract(NEW.after_summary_json,'$.status')=plan.status
        AND json_extract(NEW.after_summary_json,'$.trial_status')=plan.trial_status
        AND json_extract(NEW.after_summary_json,'$.trial_started_at') IS plan.trial_started_at
        AND json_extract(NEW.after_summary_json,'$.trial_ends_at') IS plan.trial_ends_at
        AND json_extract(NEW.after_summary_json,'$.trial_cycle')=plan.trial_cycle
        AND json_extract(NEW.after_summary_json,'$.state_revision')=plan.state_revision
        AND json_extract(NEW.after_summary_json,'$.quota_generation')=plan.quota_generation
        AND (
          (NEW.action='plan.created' AND NEW.before_summary_json='{}'
            AND plan.state_revision=1)
          OR (NEW.action='plan.updated'
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=9
            AND json_extract(NEW.before_summary_json,'$.state_revision')+1
              =plan.state_revision)
        )
    )
  )
)
OR (NEW.object_type='plan' AND NEW.action NOT IN('plan.created','plan.updated'))
OR (
  NEW.action='quota.adjustment_created'
  AND (
    NEW.object_type!='quota_adjustment' OR NEW.object_name IS NOT NULL
    OR NEW.before_summary_json!='{}'
    OR NOT EXISTS(
      SELECT 1 FROM quota_adjustments adjustment
      WHERE adjustment.tenant_id=NEW.tenant_id
        AND adjustment.quota_adjustment_id=NEW.object_id
        AND adjustment.actor_user_id=NEW.actor_user_id
        AND adjustment.operation_request_id=NEW.request_id
        AND adjustment.operation_trace_id=NEW.trace_id
        AND adjustment.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=8
        AND NOT EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'plan_id','quota_bucket_id','quota_type','direction','amount',
            'expected_bucket_revision','resulting_bucket_revision','reason_sha256'))
        AND json_extract(NEW.after_summary_json,'$.plan_id')=adjustment.plan_id
        AND json_extract(NEW.after_summary_json,'$.quota_bucket_id')
          =adjustment.quota_bucket_id
        AND json_extract(NEW.after_summary_json,'$.quota_type')=adjustment.quota_type
        AND json_extract(NEW.after_summary_json,'$.direction')=adjustment.direction
        AND json_extract(NEW.after_summary_json,'$.amount')=
          printf('%d.%06d',adjustment.amount_micros/1000000,
            adjustment.amount_micros%1000000)
        AND json_extract(NEW.after_summary_json,'$.expected_bucket_revision')
          =adjustment.expected_bucket_revision
        AND json_extract(NEW.after_summary_json,'$.resulting_bucket_revision')
          =adjustment.resulting_bucket_revision
        AND json_extract(NEW.after_summary_json,'$.reason_sha256')
          =metahub_sha256(adjustment.reason)
    )
  )
)
OR (NEW.object_type='quota_adjustment' AND NEW.action!='quota.adjustment_created')
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_policy_template_owner_insert_prd068
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN ('policy_template.applied','policy_template.rolled_back')
  AND (
    NEW.object_type!='policy_template_application'
    OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM policy_template_applications application
      JOIN policy_templates template
        ON template.tenant_id=application.tenant_id
       AND template.policy_template_id=application.policy_template_id
      WHERE application.tenant_id=NEW.tenant_id
        AND application.application_id=NEW.object_id
        AND (
          (NEW.action='policy_template.applied'
            AND application.status IN ('applied','rolled_back')
            AND application.apply_audit_log_id=NEW.audit_log_id
            AND application.actor_user_id=NEW.actor_user_id
            AND application.request_id=NEW.request_id
            AND application.trace_id=NEW.trace_id
            AND application.applied_as_of=NEW.created_at)
          OR
          (NEW.action='policy_template.rolled_back'
            AND application.status='rolled_back'
            AND application.rollback_apply_audit_log_id=NEW.audit_log_id
            AND application.rollback_actor_user_id=NEW.actor_user_id
            AND application.rollback_request_id=NEW.request_id
            AND application.rollback_trace_id=NEW.trace_id
            AND application.rolled_back_at=NEW.created_at)
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN (
            'status','template_code','template_version','application_id',
            'generated_policy_id','previous_snapshot_version','result_snapshot_version',
            'rollback_snapshot_version','impact_hash','bindings_hash','changed_resource_types'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'status','template_code','template_version','application_id',
            'generated_policy_id','previous_snapshot_version','result_snapshot_version',
            'rollback_snapshot_version','impact_hash','bindings_hash','changed_resource_types'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.value NOT IN (
            'status','template_code','template_version','application_id',
            'generated_policy_id','previous_snapshot_version','result_snapshot_version',
            'rollback_snapshot_version','impact_hash','bindings_hash','changed_resource_types'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.template_code')=template.template_code
        AND json_extract(NEW.after_summary_json,'$.template_version')=application.template_version
        AND json_extract(NEW.after_summary_json,'$.application_id')=application.application_id
        AND json_extract(NEW.after_summary_json,'$.generated_policy_id')=application.generated_policy_id
        AND json_extract(NEW.after_summary_json,'$.previous_snapshot_version')
          =application.previous_snapshot_version
        AND json_extract(NEW.after_summary_json,'$.result_snapshot_version')
          =application.result_snapshot_version
        AND json_extract(NEW.after_summary_json,'$.impact_hash')=application.impact_hash
        AND json_extract(NEW.after_summary_json,'$.bindings_hash')=application.bindings_hash
        AND json_extract(NEW.after_summary_json,'$.changed_resource_types')
          ='policy,policy_snapshot'
        AND (
          (
            NEW.action='policy_template.applied'
            AND NEW.before_summary_json='{}'
            AND json_extract(NEW.after_summary_json,'$.status')='applied'
            AND json_type(NEW.after_summary_json,'$.rollback_snapshot_version') IS NULL
          )
          OR
          (
            NEW.action='policy_template.rolled_back'
            AND json_extract(NEW.before_summary_json,'$.status')='applied'
            AND json_extract(NEW.before_summary_json,'$.template_code')=template.template_code
            AND json_extract(NEW.before_summary_json,'$.template_version')
              =application.template_version
            AND json_extract(NEW.before_summary_json,'$.application_id')
              =application.application_id
            AND json_extract(NEW.before_summary_json,'$.generated_policy_id')
              =application.generated_policy_id
            AND json_extract(NEW.before_summary_json,'$.previous_snapshot_version')
              =application.previous_snapshot_version
            AND json_extract(NEW.before_summary_json,'$.result_snapshot_version')
              =application.result_snapshot_version
            AND json_extract(NEW.before_summary_json,'$.impact_hash')=application.impact_hash
            AND json_extract(NEW.before_summary_json,'$.bindings_hash')=application.bindings_hash
            AND json_extract(NEW.before_summary_json,'$.changed_resource_types')
              ='policy,policy_snapshot'
            AND json_type(NEW.before_summary_json,'$.rollback_snapshot_version') IS NULL
            AND json_extract(NEW.after_summary_json,'$.status')='rolled_back'
            AND json_extract(NEW.after_summary_json,'$.rollback_snapshot_version')
              =application.rollback_snapshot_version
          )
        )
    )
  )
)
OR (
  NEW.object_type='policy_template_application'
  AND NEW.action NOT IN ('policy_template.applied','policy_template.rolled_back')
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
    OR (
      NEW.action IN ('config.imported','config.exported')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action IN (
        'prompt_template.created','prompt_template.updated',
        'prompt_template.version_published'
      )
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action='experiment.created' AND current.action=NEW.action
      AND current.tenant_id=NEW.tenant_id AND current.object_id=NEW.object_id
    )
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_product_event_insert_prd085
AFTER INSERT ON audit_logs FOR EACH ROW
BEGIN
  -- 常规审计动作按白名单映射为当前租户的实时激活事实。
  INSERT INTO product_events(
    product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
    operation_request_id,operation_trace_id,capture_mode,status,
    occurred_at,created_at,updated_at)
  SELECT
    'pev_'||substr(metahub_sha256(json_array(
      NEW.tenant_id,
      CASE NEW.action
        WHEN 'provider.created' THEN 'provider.created'
        WHEN 'virtual_key.created' THEN 'virtual_key.created'
        WHEN 'budget_rule.created' THEN 'budget.configured'
        WHEN 'budget_rule.updated' THEN 'budget.configured'
        WHEN 'user.invited' THEN 'teammate.invited' END,
      'audit_log',NEW.audit_log_id)),1,32),
    NEW.tenant_id,
    CASE NEW.action
      WHEN 'provider.created' THEN 'provider.created'
      WHEN 'virtual_key.created' THEN 'virtual_key.created'
      WHEN 'budget_rule.created' THEN 'budget.configured'
      WHEN 'budget_rule.updated' THEN 'budget.configured'
      WHEN 'user.invited' THEN 'teammate.invited' END,
    NEW.actor_user_id,'audit_log',NEW.audit_log_id,NEW.request_id,NEW.trace_id,
    'live','recorded',NEW.created_at,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE NEW.action IN(
    'provider.created','virtual_key.created','budget_rule.created',
    'budget_rule.updated','user.invited')
    AND NOT EXISTS(SELECT 1 FROM product_events current
      WHERE current.tenant_id=NEW.tenant_id AND current.source_kind='audit_log'
        AND current.source_id=NEW.audit_log_id
        AND current.event_type=CASE NEW.action
          WHEN 'provider.created' THEN 'provider.created'
          WHEN 'virtual_key.created' THEN 'virtual_key.created'
          WHEN 'budget_rule.created' THEN 'budget.configured'
          WHEN 'budget_rule.updated' THEN 'budget.configured'
          WHEN 'user.invited' THEN 'teammate.invited' END);

  -- 配置导入确实创建 Provider 时生成一条去重后的实时事实。
  INSERT INTO product_events(
    product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
    operation_request_id,operation_trace_id,capture_mode,status,
    occurred_at,created_at,updated_at)
  SELECT
    'pev_'||substr(metahub_sha256(json_array(
      NEW.tenant_id,'provider.created','audit_log',NEW.audit_log_id)),1,32),
    NEW.tenant_id,'provider.created',NEW.actor_user_id,'audit_log',NEW.audit_log_id,
    NEW.request_id,NEW.trace_id,'live','recorded',NEW.created_at,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE NEW.action='config.imported'
    AND json_type(NEW.after_summary_json,'$.providers_created_count')='integer'
    AND json_extract(NEW.after_summary_json,'$.providers_created_count')>0
    AND NOT EXISTS(SELECT 1 FROM product_events current
      WHERE current.tenant_id=NEW.tenant_id AND current.source_kind='audit_log'
        AND current.source_id=NEW.audit_log_id AND current.event_type='provider.created');

  -- 配置导入确实创建虚拟密钥时生成一条去重后的实时事实。
  INSERT INTO product_events(
    product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
    operation_request_id,operation_trace_id,capture_mode,status,
    occurred_at,created_at,updated_at)
  SELECT
    'pev_'||substr(metahub_sha256(json_array(
      NEW.tenant_id,'virtual_key.created','audit_log',NEW.audit_log_id)),1,32),
    NEW.tenant_id,'virtual_key.created',NEW.actor_user_id,'audit_log',NEW.audit_log_id,
    NEW.request_id,NEW.trace_id,'live','recorded',NEW.created_at,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE NEW.action='config.imported'
    AND json_type(NEW.after_summary_json,'$.virtual_keys_created_count')='integer'
    AND json_extract(NEW.after_summary_json,'$.virtual_keys_created_count')>0
    AND NOT EXISTS(SELECT 1 FROM product_events current
      WHERE current.tenant_id=NEW.tenant_id AND current.source_kind='audit_log'
        AND current.source_id=NEW.audit_log_id
        AND current.event_type='virtual_key.created');
END;
CREATE TRIGGER trg_audit_logs_response_cache_owner_insert_prd076
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN (
    'response_cache.policy_created','response_cache.policy_updated'
  )
  AND (
    NEW.object_type!='response_cache_policy' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS(
      SELECT 1 FROM response_cache_policy_events event
      JOIN response_cache_policies policy
        ON policy.tenant_id=event.tenant_id
       AND policy.cache_policy_id=event.cache_policy_id
      JOIN users actor
        ON actor.tenant_id=event.tenant_id
       AND actor.user_id=event.actor_user_id
      WHERE event.tenant_id=NEW.tenant_id
        AND event.cache_policy_id=NEW.object_id
        AND event.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND event.operation_request_id=NEW.request_id
        AND event.operation_trace_id=NEW.trace_id
        AND event.created_at=NEW.created_at
        AND event.event_type=CASE NEW.action
          WHEN 'response_cache.policy_created' THEN 'policy_created'
          ELSE 'policy_updated' END
        AND event.before_json=NEW.before_summary_json
        AND event.after_json=NEW.after_summary_json
        AND event.changed=1
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN(
            'tenant_id','cache_policy_id','model_alias_id','status','data_class',
            'ttl_seconds','charge_cache_hits','tags','version',
            'invalidation_generation'
          )
        )
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'tenant_id','cache_policy_id','model_alias_id','status','data_class',
            'ttl_seconds','charge_cache_hits','tags','version',
            'invalidation_generation'
          )
        )
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.type!='text' OR field.value NOT IN(
            'status','data_class','ttl_seconds','charge_cache_hits','tags',
            'version','invalidation_generation'
          )
        )
      )
  )
)
OR (
  NEW.action='response_cache.invalidated'
  AND (
    NEW.object_type!='response_cache_invalidation' OR NEW.object_name IS NOT NULL
    OR NEW.before_summary_json!='{}'
    OR NOT EXISTS(
      SELECT 1 FROM response_cache_invalidations invalidation
      JOIN users actor
        ON actor.tenant_id=invalidation.tenant_id
       AND actor.user_id=invalidation.actor_user_id
      WHERE invalidation.tenant_id=NEW.tenant_id
        AND invalidation.cache_invalidation_id=NEW.object_id
        AND invalidation.actor_type='user'
        AND invalidation.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND invalidation.operation_request_id=NEW.request_id
        AND invalidation.operation_trace_id=NEW.trace_id
        AND invalidation.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=8
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'cache_invalidation_id','scope','match_mode','tag_count',
            'matched_entry_count','deleted_entry_count','fenced_fill_count',
            'reason_code'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.cache_invalidation_id')
            =invalidation.cache_invalidation_id
        AND json_extract(NEW.after_summary_json,'$.scope')=invalidation.scope
        AND json_extract(NEW.after_summary_json,'$.match_mode')
            =invalidation.match_mode
        AND json_extract(NEW.after_summary_json,'$.tag_count')
            =invalidation.tag_count
        AND json_extract(NEW.after_summary_json,'$.matched_entry_count')
            =invalidation.matched_entry_count
        AND json_extract(NEW.after_summary_json,'$.deleted_entry_count')
            =invalidation.deleted_entry_count
        AND json_extract(NEW.after_summary_json,'$.fenced_fill_count')
            =invalidation.fenced_fill_count
        AND json_extract(NEW.after_summary_json,'$.reason_code')
            =invalidation.reason_code
      )
  )
)
OR (
  NEW.object_type='response_cache_policy'
  AND NEW.action NOT IN(
    'response_cache.policy_created','response_cache.policy_updated'
  )
)
OR (
  NEW.object_type='response_cache_invalidation'
  AND NEW.action!='response_cache.invalidated'
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_session_risk_owner_insert_prd070
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN (
    'session_risk.freeze_session','session_risk.freeze_virtual_key',
    'session_risk.dismiss','session_risk.release_session',
    'session_risk.release_virtual_key'
  )
  AND (
    NEW.object_type!='session_risk'
    OR NEW.object_name NOT IN ('enterprise_chat','api_session')
    OR NOT EXISTS (
      SELECT 1
      FROM session_risk_actions action
      JOIN session_risk_scores score
        ON score.tenant_id=action.tenant_id
       AND score.session_risk_score_id=action.session_risk_score_id
      JOIN session_risk_events event
        ON event.tenant_id=action.tenant_id
       AND event.session_risk_action_id=action.session_risk_action_id
      WHERE action.tenant_id=NEW.tenant_id
        AND action.session_risk_score_id=NEW.object_id
        AND action.actor_type='user'
        AND action.actor_id=NEW.actor_user_id
        AND action.source_request_id=NEW.request_id
        AND action.source_trace_id=NEW.trace_id
        AND action.created_at=NEW.created_at
        AND 'session_risk.' || action.action=NEW.action
        AND score.scope_type=NEW.object_name
        AND event.session_risk_score_id=action.session_risk_score_id
        AND event.session_risk_freeze_id IS action.session_risk_freeze_id
        AND event.event_type='security.session_high_risk'
        AND event.scope_type=score.scope_type
        AND event.scope_key=score.scope_key
        AND event.status=action.status_after
        AND event.target_type IS CASE action.action
          WHEN 'freeze_session' THEN 'session'
          WHEN 'release_session' THEN 'session'
          WHEN 'freeze_virtual_key' THEN 'virtual_key'
          WHEN 'release_virtual_key' THEN 'virtual_key'
          ELSE NULL END
        AND event.score=action.score_snapshot
        AND event.score_scale=score.score_scale
        AND event.state_revision=action.state_revision
        AND event.threshold_version=score.threshold_version
        AND event.actor_type='user'
        AND event.actor_id=action.actor_id
        AND event.source_request_id=action.source_request_id
        AND event.source_trace_id=action.source_trace_id
        AND event.occurred_at=action.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=6
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=6
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN (
            'scope_type','status','score','state_revision',
            'session_risk_freeze_id','target_type'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'scope_type','status','score','state_revision',
            'session_risk_freeze_id','target_type'
          )
        )
        AND json_extract(NEW.before_summary_json,'$.scope_type')=score.scope_type
        AND json_extract(NEW.after_summary_json,'$.scope_type')=score.scope_type
        AND json_extract(NEW.before_summary_json,'$.status')=action.status_before
        AND json_extract(NEW.after_summary_json,'$.status')=action.status_after
        AND json_extract(NEW.before_summary_json,'$.score')=action.score_snapshot
        AND json_extract(NEW.after_summary_json,'$.score')=action.score_snapshot
        AND json_extract(NEW.before_summary_json,'$.state_revision')=action.state_revision-1
        AND json_extract(NEW.after_summary_json,'$.state_revision')=action.state_revision
        AND json_extract(NEW.before_summary_json,'$.session_risk_freeze_id')
          IS CASE WHEN action.action IN ('release_session','release_virtual_key')
             THEN action.session_risk_freeze_id ELSE NULL END
        AND json_extract(NEW.after_summary_json,'$.session_risk_freeze_id')
          IS action.session_risk_freeze_id
        AND json_extract(NEW.before_summary_json,'$.target_type')
          IS CASE action.action
             WHEN 'release_session' THEN 'session'
             WHEN 'release_virtual_key' THEN 'virtual_key'
             ELSE NULL END
        AND json_extract(NEW.after_summary_json,'$.target_type')
          IS CASE action.action
             WHEN 'freeze_session' THEN 'session'
             WHEN 'release_session' THEN 'session'
             WHEN 'freeze_virtual_key' THEN 'virtual_key'
             WHEN 'release_virtual_key' THEN 'virtual_key'
             ELSE NULL END
        AND (SELECT COUNT(*) FROM json_each(NEW.changed_fields_json))
          =(SELECT COUNT(DISTINCT value) FROM json_each(NEW.changed_fields_json))
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.type!='text'
             OR field.value NOT IN (
               'scope_type','status','score','state_revision',
               'session_risk_freeze_id','target_type'
             )
        )
        AND NOT EXISTS (
          SELECT 1
          FROM json_each(
            '["scope_type","status","score","state_revision",' ||
            '"session_risk_freeze_id","target_type"]'
          ) expected
          WHERE (
            json_extract(NEW.before_summary_json,'$.' || expected.value)
              IS NOT json_extract(NEW.after_summary_json,'$.' || expected.value)
          ) != EXISTS (
            SELECT 1 FROM json_each(NEW.changed_fields_json) actual
            WHERE actual.value=expected.value
          )
        )
    )
  )
)
OR (
  NEW.object_type='session_risk'
  AND NEW.action NOT IN (
    'session_risk.freeze_session','session_risk.freeze_virtual_key',
    'session_risk.dismiss','session_risk.release_session',
    'session_risk.release_virtual_key'
  )
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
UPDATE tenant_deletion_table_manifest SET table_sql_sha256=metahub_sha256((SELECT sql FROM sqlite_schema WHERE name='audit_logs' AND type='table')) WHERE source_table='audit_logs';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
DROP TABLE virtual_key_consumers;
DROP TABLE workspace_consumers;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
UPDATE tenant_deletion_table_manifest SET
  table_sql_sha256=metahub_sha256((SELECT sql FROM sqlite_schema WHERE name='virtual_keys' AND type='table')),
  column_signature_sha256=metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info('virtual_keys') ORDER BY cid)))
WHERE source_table='virtual_keys';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
COMMIT;
PRAGMA foreign_keys = ON;
