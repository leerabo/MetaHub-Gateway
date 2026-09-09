ALTER TABLE virtual_keys ADD COLUMN credential_revision INTEGER NOT NULL DEFAULT 1 CHECK(credential_revision>0);
CREATE TABLE workspace_consumers (
  consumer_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 160),
  department TEXT NOT NULL DEFAULT '' CHECK(length(department)<=160),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN('active','disabled')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id,consumer_id),
  FOREIGN KEY(tenant_id,created_by) REFERENCES users(tenant_id,user_id)
);
CREATE INDEX idx_consumers_tenant_directory ON workspace_consumers(tenant_id,created_at,consumer_id);
CREATE FUNCTION fn_consumers_update() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.consumer_id!=OLD.consumer_id OR NEW.tenant_id!=OLD.tenant_id
    OR NEW.created_by!=OLD.created_by OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='consumer_identity_immutable'; END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION fn_consumers_immutable() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='consumer_identity_immutable'; END $$;
CREATE TRIGGER trg_consumers_update BEFORE UPDATE ON workspace_consumers
FOR EACH ROW EXECUTE FUNCTION fn_consumers_update();
CREATE TRIGGER trg_consumers_delete BEFORE DELETE ON workspace_consumers
FOR EACH ROW EXECUTE FUNCTION fn_consumers_immutable();

CREATE TABLE virtual_key_consumers (
  virtual_key_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  consumer_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,virtual_key_id) REFERENCES virtual_keys(tenant_id,virtual_key_id),
  FOREIGN KEY(tenant_id,consumer_id) REFERENCES workspace_consumers(tenant_id,consumer_id)
);
CREATE INDEX idx_key_consumers_owner ON virtual_key_consumers(tenant_id,consumer_id,virtual_key_id);
CREATE FUNCTION fn_key_consumers_insert() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS(SELECT 1 FROM workspace_consumers c
      WHERE c.tenant_id=NEW.tenant_id AND c.consumer_id=NEW.consumer_id AND c.status='active')
    OR EXISTS(SELECT 1 FROM request_logs r WHERE r.tenant_id=NEW.tenant_id AND r.virtual_key_id=NEW.virtual_key_id)
    OR EXISTS(SELECT 1 FROM usage_ledger u WHERE u.tenant_id=NEW.tenant_id AND u.virtual_key_id=NEW.virtual_key_id)
  THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='consumer_binding_invalid'; END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION fn_key_consumers_immutable() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='consumer_binding_immutable'; END $$;
CREATE TRIGGER trg_key_consumers_insert BEFORE INSERT ON virtual_key_consumers
FOR EACH ROW EXECUTE FUNCTION fn_key_consumers_insert();
CREATE TRIGGER trg_key_consumers_update BEFORE UPDATE ON virtual_key_consumers
FOR EACH ROW EXECUTE FUNCTION fn_key_consumers_immutable();
CREATE TRIGGER trg_key_consumers_delete BEFORE DELETE ON virtual_key_consumers
FOR EACH ROW EXECUTE FUNCTION fn_key_consumers_immutable();

DO $$ DECLARE table_name TEXT; BEGIN
FOREACH table_name IN ARRAY ARRAY['workspace_consumers','virtual_key_consumers'] LOOP
  WITH columns AS (
    SELECT a.attnum,a.attname,format_type(a.atttypid,a.atttypmod) AS type_name,
      a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default_sql
    FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
    WHERE a.attrelid=table_name::regclass AND a.attnum>0 AND NOT a.attisdropped
  ), definitions AS (
    SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
      ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
      ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
    UNION ALL
    SELECT 10000+row_number() OVER(ORDER BY conname),
      'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
    FROM pg_constraint WHERE conrelid=table_name::regclass
  ), triggers AS (
    SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
    FROM pg_trigger WHERE tgrelid=table_name::regclass AND NOT tgisinternal
  )
  INSERT INTO tenant_deletion_table_manifest(
    source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
    trigger_set_sha256,catalog_revision,created_at)
  SELECT table_name,'archive_then_delete','security',
    metahub_sha256('CREATE TABLE '||table_name||' ('
      ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')'),
    metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
      ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum) FROM columns)),
    metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30) ORDER BY tgname) FROM triggers),'')),
    1,CURRENT_TIMESTAMP;
  INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
  SELECT tgname,table_name,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid),
    metahub_sha256(pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid))
  FROM pg_trigger WHERE tgrelid=table_name::regclass AND NOT tgisinternal;
END LOOP;
END $$;

ALTER TABLE audit_logs DROP CONSTRAINT ck_audit_logs_2;
ALTER TABLE audit_logs ADD CONSTRAINT ck_audit_logs_2 CHECK(action IN (
      'provider.created','provider.updated',
      'provider_key.created','provider_key.updated',
      'virtual_key.created','virtual_key.updated','virtual_key.rotated','consumer.created','consumer.updated',
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
      'consumer','provider','provider_key','virtual_key','budget_rule','dlp_rule',
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
