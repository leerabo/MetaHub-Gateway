-- Restore SQLite's deferred referential contracts lost by the 0139 generator.
-- Do not rewrite the applied baseline, remove constraints, or modify business rows.
-- Resolve by ordered columns, since later table rebuilds may rename constraints.
DO $repair$
DECLARE
  contract RECORD;
  constraint_count INTEGER;
  constraint_name TEXT;
BEGIN
  FOR contract IN
    SELECT * FROM (VALUES
    ('approval_tasks', ARRAY['tenant_id','mcp_invocation_id']::text[], 'mcp_invocations', ARRAY['tenant_id','mcp_invocation_id']::text[]),
    ('assistant_audits', ARRAY['tenant_id','audit_log_id']::text[], 'audit_logs', ARRAY['tenant_id','audit_log_id']::text[]),
    ('audit_exports', ARRAY['tenant_id','audit_log_id']::text[], 'audit_logs', ARRAY['tenant_id','audit_log_id']::text[]),
    ('edge_backup_statuses', ARRAY['latest_backup_id']::text[], 'edge_backup_reports', ARRAY['edge_backup_id']::text[]),
    ('edge_licenses', ARRAY['current_entitlement_id']::text[], 'edge_signed_entitlements', ARRAY['entitlement_id']::text[]),
    ('experiment_assignments', ARRAY['tenant_id','request_id']::text[], 'gateway_request_scope_claims', ARRAY['tenant_id','request_id']::text[]),
    ('experiment_assignments', ARRAY['tenant_id','assignment_id','execution_id']::text[], 'experiment_request_executions', ARRAY['tenant_id','assignment_id','execution_id']::text[]),
    ('experiment_assignments', ARRAY['tenant_id','assignment_id','usage_settlement_id']::text[], 'experiment_usage_settlements', ARRAY['tenant_id','assignment_id','usage_settlement_id']::text[]),
    ('file_security_findings', ARRAY['tenant_id','scan_id','file_id']::text[], 'file_security_scans', ARRAY['tenant_id','scan_id','file_id']::text[]),
    ('mcp_invocations', ARRAY['tenant_id','approval_task_id','mcp_invocation_id','approval_gate_status','requested_by','approval_gate_target_version','policy_fingerprint','approval_deadline_at']::text[], 'approval_tasks', ARRAY['tenant_id','approval_task_id','mcp_invocation_id','status','created_by_id','target_version','target_fingerprint','expires_at']::text[]),
    ('mcp_network_policies', ARRAY['tenant_id','mcp_network_policy_id','mcp_server_id','current_revision','current_fingerprint','status']::text[], 'mcp_network_policy_revisions', ARRAY['tenant_id','mcp_network_policy_id','mcp_server_id','policy_revision','policy_fingerprint','status']::text[]),
    ('mcp_server_credentials', ARRAY['tenant_id','probe_operation_id']::text[], 'mcp_server_operations', ARRAY['tenant_id','mcp_server_operation_id']::text[]),
    ('mcp_server_operations', ARRAY['tenant_id','mcp_server_id','candidate_credential_revision']::text[], 'mcp_server_credentials', ARRAY['tenant_id','mcp_server_id','credential_revision']::text[]),
    ('mcp_servers', ARRAY['tenant_id','mcp_server_id','active_credential_revision']::text[], 'mcp_server_credentials', ARRAY['tenant_id','mcp_server_id','credential_revision']::text[]),
    ('policy_template_applications', ARRAY['tenant_id','apply_audit_log_id']::text[], 'audit_logs', ARRAY['tenant_id','audit_log_id']::text[]),
    ('policy_template_applications', ARRAY['tenant_id','template_event_id']::text[], 'policy_template_events', ARRAY['tenant_id','policy_template_event_id']::text[]),
    ('policy_template_applications', ARRAY['tenant_id','rollback_snapshot_id','rollback_snapshot_version']::text[], 'policy_snapshots', ARRAY['tenant_id','policy_snapshot_id','snapshot_version']::text[]),
    ('policy_template_applications', ARRAY['tenant_id','rollback_policy_audit_log_id']::text[], 'audit_logs', ARRAY['tenant_id','audit_log_id']::text[]),
    ('policy_template_applications', ARRAY['tenant_id','rollback_snapshot_audit_log_id']::text[], 'audit_logs', ARRAY['tenant_id','audit_log_id']::text[]),
    ('policy_template_applications', ARRAY['tenant_id','rollback_apply_audit_log_id']::text[], 'audit_logs', ARRAY['tenant_id','audit_log_id']::text[]),
    ('policy_template_applications', ARRAY['tenant_id','rollback_snapshot_event_id']::text[], 'policy_snapshot_events', ARRAY['tenant_id','policy_snapshot_event_id']::text[]),
    ('prompt_templates', ARRAY['tenant_id','prompt_template_id','current_version_id','current_version_state']::text[], 'prompt_template_versions', ARRAY['tenant_id','prompt_template_id','prompt_template_version_id','state']::text[]),
    ('provider_error_map_operations', ARRAY['tenant_id','target_provider_error_map_id']::text[], 'provider_error_maps', ARRAY['tenant_id','provider_error_map_id']::text[]),
    ('provider_error_map_versions', ARRAY['tenant_id','provider_error_map_id']::text[], 'provider_error_maps', ARRAY['tenant_id','provider_error_map_id']::text[]),
    ('provider_error_maps', ARRAY['tenant_id','provider_error_map_id','current_version']::text[], 'provider_error_map_versions', ARRAY['tenant_id','provider_error_map_id','version']::text[]),
    ('request_feedback_events', ARRAY['tenant_id','request_feedback_id']::text[], 'request_feedbacks', ARRAY['tenant_id','request_feedback_id']::text[]),
    ('request_feedbacks', ARRAY['tenant_id','feedback_event_id']::text[], 'request_feedback_events', ARRAY['tenant_id','feedback_event_id']::text[]),
    ('role_dashboard_configs', ARRAY['tenant_id','role_code']::text[], 'roles', ARRAY['tenant_id','code']::text[]),
    ('tenant_data_states', ARRAY['tenant_id','active_deletion_request_id']::text[], 'deletion_requests', ARRAY['tenant_id','deletion_request_id']::text[])
    ) AS expected(source_table,source_columns,target_table,target_columns)
  LOOP
    SELECT count(*),min(c.conname::text)
      INTO constraint_count,constraint_name
      FROM pg_constraint c
      WHERE c.contype='f'
        AND c.conrelid=to_regclass(format('public.%I',contract.source_table))
        AND c.confrelid=to_regclass(format('public.%I',contract.target_table))
        AND ARRAY(SELECT a.attname::text
                  FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum,position)
                  JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
                  ORDER BY k.position)=contract.source_columns
        AND ARRAY(SELECT a.attname::text
                  FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum,position)
                  JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.attnum
                  ORDER BY k.position)=contract.target_columns;
    IF constraint_count!=1 THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='deferred_foreign_key_contract_missing';
    END IF;
    EXECUTE format('ALTER TABLE public.%I ALTER CONSTRAINT %I DEFERRABLE INITIALLY DEFERRED',
                   contract.source_table,constraint_name);
  END LOOP;
END
$repair$;
