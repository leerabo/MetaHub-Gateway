DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM workspace_consumers) OR EXISTS(SELECT 1 FROM virtual_key_consumers)
    OR EXISTS(SELECT 1 FROM audit_logs WHERE action IN('consumer.created','consumer.updated','virtual_key.rotated'))
  THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='consumer_rollback_blocked'; END IF;
END $$;
ALTER TABLE virtual_keys DROP COLUMN credential_revision;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089 ON tenant_deletion_trigger_manifest;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089 ON tenant_deletion_table_manifest;
DELETE FROM tenant_deletion_trigger_manifest WHERE source_table IN('workspace_consumers','virtual_key_consumers');
DELETE FROM tenant_deletion_table_manifest WHERE source_table IN('workspace_consumers','virtual_key_consumers');
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089 BEFORE DELETE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_delete_prd089();
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089 BEFORE DELETE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_delete_prd089();
DROP TABLE virtual_key_consumers;
DROP TABLE workspace_consumers;
DROP FUNCTION fn_key_consumers_insert();
DROP FUNCTION fn_key_consumers_immutable();
DROP FUNCTION fn_consumers_update();
DROP FUNCTION fn_consumers_immutable();

ALTER TABLE audit_logs DROP CONSTRAINT ck_audit_logs_2;
ALTER TABLE audit_logs ADD CONSTRAINT ck_audit_logs_2 CHECK(action IN (
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
      'mcp.network_policy_updated'));
ALTER TABLE audit_logs DROP CONSTRAINT ck_audit_logs_3;
ALTER TABLE audit_logs ADD CONSTRAINT ck_audit_logs_3 CHECK(object_type IN (
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
      'mcp_server','mcp_server_operation','mcp_tool','mcp_network_policy'));

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
WITH definitions AS (
  SELECT a.attnum AS ordinal,quote_ident(a.attname)||' '||format_type(a.atttypid,a.atttypmod)
    ||CASE WHEN d.adbin IS NULL THEN '' ELSE ' DEFAULT '||pg_get_expr(d.adbin,d.adrelid) END
    ||CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END AS sql
  FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
  WHERE a.attrelid='audit_logs'::regclass AND a.attnum>0 AND NOT a.attisdropped
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='audit_logs'::regclass
)
UPDATE tenant_deletion_table_manifest SET table_sql_sha256=metahub_sha256(
  'CREATE TABLE audit_logs ('||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')')
WHERE source_table='audit_logs';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
WITH columns AS (
  SELECT a.attnum,a.attname,format_type(a.atttypid,a.atttypmod) AS type_name,
    a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default_sql
  FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
  WHERE a.attrelid='virtual_keys'::regclass AND a.attnum>0 AND NOT a.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='virtual_keys'::regclass
)
UPDATE tenant_deletion_table_manifest SET table_sql_sha256=metahub_sha256(
  'CREATE TABLE virtual_keys ('||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')'),
  column_signature_sha256=metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum) FROM columns))
WHERE source_table='virtual_keys';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();
