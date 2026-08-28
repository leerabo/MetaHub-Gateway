-- 建立私有 Gateway 备份摘要、恢复验证和隔离灾备演练事实。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd129_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd129_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0120' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'edge_deployments','edge_device_identities','workspace_memberships',
      'platform_operators','tenant_deletion_table_manifest',
      'tenant_deletion_trigger_manifest'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd129')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd129_up_guard;

CREATE TABLE workspace_edge_backup_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'edge.backup.read','edge.backup.create','edge.restore.approve','edge.drill.manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-129'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=39)
);
CREATE TRIGGER trg_workspace_edge_backup_permissions_update_prd129
BEFORE UPDATE ON workspace_edge_backup_permission_extensions
BEGIN SELECT RAISE(ABORT,'edge_backup_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_backup_permissions_delete_prd129
BEFORE DELETE ON workspace_edge_backup_permission_extensions
BEGIN SELECT RAISE(ABORT,'edge_backup_permission_immutable'); END;
CREATE TABLE workspace_edge_backup_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_edge_backup_permission_extensions(permission_code)
);
CREATE TRIGGER trg_workspace_edge_backup_roles_insert_prd129
BEFORE INSERT ON workspace_edge_backup_role_permissions FOR EACH ROW
WHEN NOT(
  NEW.role_code='owner'
  OR(NEW.role_code='admin' AND NEW.permission_code IN(
    'edge.backup.read','edge.backup.create','edge.drill.manage'))
  OR(NEW.role_code='security_admin' AND NEW.permission_code IN(
    'edge.backup.read','edge.restore.approve','edge.drill.manage'))
  OR(NEW.role_code='auditor' AND NEW.permission_code='edge.backup.read'))
BEGIN SELECT RAISE(ABORT,'edge_backup_role_permission_invalid'); END;
CREATE TRIGGER trg_workspace_edge_backup_roles_update_prd129
BEFORE UPDATE ON workspace_edge_backup_role_permissions
BEGIN SELECT RAISE(ABORT,'edge_backup_role_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_backup_roles_delete_prd129
BEFORE DELETE ON workspace_edge_backup_role_permissions
BEGIN SELECT RAISE(ABORT,'edge_backup_role_permission_immutable'); END;
INSERT INTO workspace_edge_backup_permission_extensions VALUES
  ('edge.backup.read','PRD-129',39),('edge.backup.create','PRD-129',39),
  ('edge.restore.approve','PRD-129',39),('edge.drill.manage','PRD-129',39);
INSERT INTO workspace_edge_backup_role_permissions VALUES
  ('owner','edge.backup.read'),('owner','edge.backup.create'),
  ('owner','edge.restore.approve'),('owner','edge.drill.manage'),
  ('admin','edge.backup.read'),('admin','edge.backup.create'),
  ('admin','edge.drill.manage'),('security_admin','edge.backup.read'),
  ('security_admin','edge.restore.approve'),('security_admin','edge.drill.manage'),
  ('auditor','edge.backup.read');

CREATE TABLE platform_edge_backup_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.edge_backup.read','platform.edge_drill.manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-129'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=39)
);
CREATE TRIGGER trg_platform_edge_backup_permissions_update_prd129
BEFORE UPDATE ON platform_edge_backup_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_backup_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_backup_permissions_delete_prd129
BEFORE DELETE ON platform_edge_backup_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_backup_permission_immutable'); END;
CREATE TABLE platform_edge_backup_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'platform_owner','platform_ops','platform_security','platform_billing',
    'platform_verification','platform_compliance','platform_privacy',
    'platform_support','release_manager','platform_provider_engineer','platform_channel',
    'platform_customer_success','platform_growth')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_edge_backup_permission_extensions(permission_code)
);
CREATE TRIGGER trg_platform_edge_backup_roles_insert_prd129
BEFORE INSERT ON platform_edge_backup_role_permissions FOR EACH ROW
WHEN NOT(
  NEW.role_code IN('platform_owner','platform_ops')
  OR(NEW.role_code='platform_security'
    AND NEW.permission_code='platform.edge_backup.read'))
BEGIN SELECT RAISE(ABORT,'platform_edge_backup_role_permission_invalid'); END;
CREATE TRIGGER trg_platform_edge_backup_roles_update_prd129
BEFORE UPDATE ON platform_edge_backup_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_backup_role_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_backup_roles_delete_prd129
BEFORE DELETE ON platform_edge_backup_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_backup_role_permission_immutable'); END;
INSERT INTO platform_edge_backup_permission_extensions VALUES
  ('platform.edge_backup.read','PRD-129',39),
  ('platform.edge_drill.manage','PRD-129',39);
INSERT INTO platform_edge_backup_role_permissions VALUES
  ('platform_owner','platform.edge_backup.read'),
  ('platform_owner','platform.edge_drill.manage'),
  ('platform_ops','platform.edge_backup.read'),
  ('platform_ops','platform.edge_drill.manage'),
  ('platform_security','platform.edge_backup.read');

-- 云端仅保存备份摘要；块、信封、对象 URL 和客户路径始终留在本地。
CREATE TABLE edge_backup_reports(
  edge_backup_id TEXT PRIMARY KEY CHECK(length(edge_backup_id)=36
    AND substr(edge_backup_id,1,4)='ebk_'
    AND substr(edge_backup_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  source_generation INTEGER NOT NULL CHECK(source_generation>=1),
  schema_head TEXT NOT NULL CHECK(length(schema_head) BETWEEN 4 AND 32),
  storage_kind TEXT NOT NULL CHECK(storage_kind IN('local','customer_object_store')),
  status TEXT NOT NULL CHECK(status IN('completed','failed','deleted')),
  size_bucket TEXT NOT NULL CHECK(size_bucket IN(
    'lt_100mb','100mb_1gb','1gb_10gb','gte_10gb')),
  chunk_count INTEGER NOT NULL CHECK(chunk_count BETWEEN 0 AND 1000000),
  manifest_sha256 TEXT CHECK(manifest_sha256 IS NULL OR(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*')),
  kms_key_id_sha256 TEXT CHECK(kms_key_id_sha256 IS NULL OR(length(kms_key_id_sha256)=64
    AND kms_key_id_sha256 NOT GLOB '*[^0-9a-f]*')),
  check_outcome TEXT NOT NULL CHECK(check_outcome IN('verified','failed')),
  failure_code TEXT CHECK(failure_code IS NULL OR(length(failure_code) BETWEEN 2 AND 80
    AND failure_code=lower(failure_code)
    AND failure_code NOT GLOB '*[^a-z0-9_.-]*')),
  started_at TEXT NOT NULL CHECK(unixepoch(started_at) IS NOT NULL),
  completed_at TEXT NOT NULL CHECK(unixepoch(completed_at) IS NOT NULL),
  retention_until TEXT NOT NULL CHECK(unixepoch(retention_until) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  UNIQUE(workspace_id,tenant_id,edge_backup_id),
  UNIQUE(edge_deployment_id,source_generation,manifest_sha256),
  CHECK(unixepoch(completed_at)>=unixepoch(started_at)),
  CHECK(unixepoch(retention_until)>unixepoch(completed_at)),
  CHECK((status='completed' AND check_outcome='verified' AND manifest_sha256 IS NOT NULL
      AND kms_key_id_sha256 IS NOT NULL AND chunk_count>0 AND failure_code IS NULL)
    OR(status IN('failed','deleted')))
);
CREATE INDEX idx_edge_backup_reports_owner_prd129
  ON edge_backup_reports(workspace_id,tenant_id,edge_deployment_id,completed_at DESC);
CREATE TRIGGER trg_edge_backup_reports_update_prd129
BEFORE UPDATE ON edge_backup_reports
BEGIN SELECT RAISE(ABORT,'edge_backup_report_immutable'); END;
CREATE TRIGGER trg_edge_backup_reports_delete_prd129
BEFORE DELETE ON edge_backup_reports
BEGIN SELECT RAISE(ABORT,'edge_backup_report_immutable'); END;

CREATE TABLE edge_backup_statuses(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  latest_backup_id TEXT,
  latest_source_generation INTEGER NOT NULL CHECK(latest_source_generation>=0),
  status TEXT NOT NULL CHECK(status IN('never','current','overdue','failed')),
  rpo_objective_seconds INTEGER NOT NULL CHECK(rpo_objective_seconds BETWEEN 900 AND 604800),
  last_success_at TEXT CHECK(last_success_at IS NULL OR unixepoch(last_success_at) IS NOT NULL),
  last_check_outcome TEXT CHECK(last_check_outcome IS NULL OR last_check_outcome IN(
    'verified','failed')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(latest_backup_id) REFERENCES edge_backup_reports(edge_backup_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK((status='never' AND latest_backup_id IS NULL AND latest_source_generation=0
      AND last_success_at IS NULL AND last_check_outcome IS NULL)
    OR(status!='never' AND latest_backup_id IS NOT NULL
      AND latest_source_generation>=1 AND last_check_outcome IS NOT NULL))
);
CREATE TRIGGER trg_edge_backup_status_update_prd129
BEFORE UPDATE ON edge_backup_statuses FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.revision!=OLD.revision+1
  OR NEW.latest_source_generation<OLD.latest_source_generation
  OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'edge_backup_status_transition_invalid'); END;
CREATE TRIGGER trg_edge_backup_status_delete_prd129
BEFORE DELETE ON edge_backup_statuses
BEGIN SELECT RAISE(ABORT,'edge_backup_status_immutable'); END;

CREATE TABLE edge_restore_jobs(
  restore_job_id TEXT PRIMARY KEY CHECK(length(restore_job_id)=36
    AND substr(restore_job_id,1,4)='rst_'
    AND substr(restore_job_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  source_backup_id TEXT NOT NULL,
  target_kind TEXT NOT NULL CHECK(target_kind IN('replacement','drill')),
  status TEXT NOT NULL CHECK(status IN(
    'requested','approved','running','verifying','succeeded','failed','cancelled')),
  current_step TEXT NOT NULL CHECK(current_step IN(
    'approval','staging','integrity','schema','restore','reenroll','verify','complete')),
  requested_by_account_id TEXT NOT NULL,
  approved_by_account_id TEXT,
  device_reenrollment_required INTEGER NOT NULL CHECK(device_reenrollment_required=1),
  expected_backup_generation INTEGER NOT NULL CHECK(expected_backup_generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  approved_at TEXT CHECK(approved_at IS NULL OR unixepoch(approved_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,source_backup_id)
    REFERENCES edge_backup_reports(workspace_id,tenant_id,edge_backup_id),
  FOREIGN KEY(requested_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(approved_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,restore_job_id),
  CHECK(approved_by_account_id IS NULL OR approved_by_account_id!=requested_by_account_id),
  CHECK((status='requested' AND approved_by_account_id IS NULL AND approved_at IS NULL)
    OR(status!='requested' AND approved_by_account_id IS NOT NULL AND approved_at IS NOT NULL)),
  CHECK((status IN('succeeded','failed','cancelled') AND completed_at IS NOT NULL)
    OR(status NOT IN('succeeded','failed','cancelled') AND completed_at IS NULL))
);
CREATE INDEX idx_edge_restore_jobs_owner_prd129
  ON edge_restore_jobs(workspace_id,tenant_id,edge_deployment_id,updated_at DESC);
CREATE TRIGGER trg_edge_restore_jobs_update_prd129
BEFORE UPDATE ON edge_restore_jobs FOR EACH ROW
WHEN NEW.restore_job_id!=OLD.restore_job_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.source_backup_id!=OLD.source_backup_id OR NEW.target_kind!=OLD.target_kind
  OR NEW.requested_by_account_id!=OLD.requested_by_account_id
  OR NEW.expected_backup_generation!=OLD.expected_backup_generation
  OR NEW.requested_at!=OLD.requested_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR OLD.status IN('succeeded','failed','cancelled')
BEGIN SELECT RAISE(ABORT,'edge_restore_job_transition_invalid'); END;
CREATE TRIGGER trg_edge_restore_jobs_delete_prd129
BEFORE DELETE ON edge_restore_jobs
BEGIN SELECT RAISE(ABORT,'edge_restore_job_immutable'); END;

CREATE TABLE edge_restore_verifications(
  restore_verification_id TEXT PRIMARY KEY CHECK(length(restore_verification_id)=36
    AND substr(restore_verification_id,1,4)='rvf_'
    AND substr(restore_verification_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  restore_job_id TEXT NOT NULL,
  verification_code TEXT NOT NULL CHECK(verification_code IN(
    'manifest_integrity','owner_binding','schema_compatibility','config_consistency',
    'usage_cursor','audit_chain','secret_reference','device_reenrollment','minimal_request')),
  outcome TEXT NOT NULL CHECK(outcome IN('passed','failed')),
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64
    AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
  checked_at TEXT NOT NULL CHECK(unixepoch(checked_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,restore_job_id)
    REFERENCES edge_restore_jobs(workspace_id,tenant_id,restore_job_id),
  UNIQUE(restore_job_id,verification_code)
);
CREATE TRIGGER trg_edge_restore_verifications_update_prd129
BEFORE UPDATE ON edge_restore_verifications
BEGIN SELECT RAISE(ABORT,'edge_restore_verification_immutable'); END;
CREATE TRIGGER trg_edge_restore_verifications_delete_prd129
BEFORE DELETE ON edge_restore_verifications
BEGIN SELECT RAISE(ABORT,'edge_restore_verification_immutable'); END;

CREATE TABLE private_dr_drills(
  private_dr_drill_id TEXT PRIMARY KEY CHECK(length(private_dr_drill_id)=36
    AND substr(private_dr_drill_id,1,4)='pdd_'
    AND substr(private_dr_drill_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  restore_job_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('requested','approved','running','succeeded','failed')),
  quarter_code TEXT NOT NULL CHECK(quarter_code GLOB '[0-9][0-9][0-9][0-9]-Q[1-4]'),
  rpo_objective_seconds INTEGER NOT NULL CHECK(rpo_objective_seconds BETWEEN 900 AND 604800),
  rto_objective_seconds INTEGER NOT NULL CHECK(rto_objective_seconds BETWEEN 900 AND 604800),
  actual_rpo_seconds INTEGER CHECK(actual_rpo_seconds IS NULL OR actual_rpo_seconds>=0),
  actual_rto_seconds INTEGER CHECK(actual_rto_seconds IS NULL OR actual_rto_seconds>=0),
  isolated_target_sha256 TEXT NOT NULL CHECK(length(isolated_target_sha256)=64
    AND isolated_target_sha256 NOT GLOB '*[^0-9a-f]*'),
  production_untouched INTEGER NOT NULL CHECK(production_untouched=1),
  objective_outcome TEXT CHECK(objective_outcome IS NULL OR objective_outcome IN(
    'met','missed')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,restore_job_id)
    REFERENCES edge_restore_jobs(workspace_id,tenant_id,restore_job_id),
  UNIQUE(workspace_id,tenant_id,private_dr_drill_id),
  CHECK((status IN('succeeded','failed') AND objective_outcome IS NOT NULL
      AND actual_rpo_seconds IS NOT NULL AND actual_rto_seconds IS NOT NULL
      AND completed_at IS NOT NULL)
    OR(status NOT IN('succeeded','failed') AND objective_outcome IS NULL
      AND completed_at IS NULL))
);
CREATE INDEX idx_private_dr_drills_owner_prd129
  ON private_dr_drills(workspace_id,tenant_id,edge_deployment_id,created_at DESC);
CREATE TRIGGER trg_private_dr_drills_update_prd129
BEFORE UPDATE ON private_dr_drills FOR EACH ROW
WHEN NEW.private_dr_drill_id!=OLD.private_dr_drill_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.restore_job_id!=OLD.restore_job_id OR NEW.quarter_code!=OLD.quarter_code
  OR NEW.rpo_objective_seconds!=OLD.rpo_objective_seconds
  OR NEW.rto_objective_seconds!=OLD.rto_objective_seconds
  OR NEW.isolated_target_sha256!=OLD.isolated_target_sha256
  OR NEW.production_untouched!=OLD.production_untouched
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at OR OLD.status IN('succeeded','failed')
BEGIN SELECT RAISE(ABORT,'edge_dr_drill_transition_invalid'); END;
CREATE TRIGGER trg_private_dr_drills_delete_prd129
BEFORE DELETE ON private_dr_drills
BEGIN SELECT RAISE(ABORT,'edge_dr_drill_immutable'); END;

CREATE TABLE edge_backup_events(
  edge_backup_event_id TEXT PRIMARY KEY CHECK(length(edge_backup_event_id)=36
    AND substr(edge_backup_event_id,1,4)='bev_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'edge.backup_completed','edge.backup_failed','edge.restore_requested',
    'edge.restore_approved','edge.restore_completed','edge.restore_failed',
    'edge.drill_requested','edge.drill_completed','edge.dr_objective_missed')),
  object_type TEXT NOT NULL CHECK(object_type IN('backup','restore','drill')),
  object_id TEXT NOT NULL CHECK(length(object_id)=36),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('workspace','agent','system')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 2 AND 160),
  projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  UNIQUE(workspace_id,tenant_id,edge_backup_event_id)
);
CREATE TRIGGER trg_edge_backup_events_update_prd129 BEFORE UPDATE ON edge_backup_events
BEGIN SELECT RAISE(ABORT,'edge_backup_event_immutable'); END;
CREATE TRIGGER trg_edge_backup_events_delete_prd129 BEFORE DELETE ON edge_backup_events
BEGIN SELECT RAISE(ABORT,'edge_backup_event_immutable'); END;

CREATE TABLE edge_backup_outbox(
  edge_backup_outbox_id TEXT PRIMARY KEY CHECK(length(edge_backup_outbox_id)=36
    AND substr(edge_backup_outbox_id,1,4)='bou_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_backup_event_id TEXT NOT NULL,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('workspace','operations','security')),
  status TEXT NOT NULL CHECK(status IN('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_backup_event_id)
    REFERENCES edge_backup_events(workspace_id,tenant_id,edge_backup_event_id),
  UNIQUE(edge_backup_event_id,delivery_domain)
);
CREATE TRIGGER trg_edge_backup_outbox_update_prd129
BEFORE UPDATE ON edge_backup_outbox FOR EACH ROW
WHEN NEW.edge_backup_outbox_id!=OLD.edge_backup_outbox_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_backup_event_id!=OLD.edge_backup_event_id
  OR NEW.delivery_domain!=OLD.delivery_domain OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count!=OLD.attempt_count+1 OR NEW.updated_at<=OLD.updated_at
  OR OLD.status='delivered'
BEGIN SELECT RAISE(ABORT,'edge_backup_outbox_transition_invalid'); END;
CREATE TRIGGER trg_edge_backup_outbox_delete_prd129 BEFORE DELETE ON edge_backup_outbox
BEGIN SELECT RAISE(ABORT,'edge_backup_outbox_immutable'); END;

CREATE TABLE edge_backup_action_commands(
  edge_backup_action_command_id TEXT PRIMARY KEY CHECK(length(edge_backup_action_command_id)=36
    AND substr(edge_backup_action_command_id,1,4)='bac_'),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('workspace','agent')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 2 AND 160),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'edge.backup.report','edge.drill.request','edge.restore.approve','edge.drill.report')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(actor_domain,actor_id,action_code,idempotency_sha256)
);
CREATE TRIGGER trg_edge_backup_actions_update_prd129
BEFORE UPDATE ON edge_backup_action_commands
BEGIN SELECT RAISE(ABORT,'edge_backup_action_immutable'); END;
CREATE TRIGGER trg_edge_backup_actions_delete_prd129
BEFORE DELETE ON edge_backup_action_commands
BEGIN SELECT RAISE(ABORT,'edge_backup_action_immutable'); END;

-- 登记所有 tenant-owned 云端摘要表，保持删除清单完整。
CREATE TABLE tenant_deletion_manifest_extensions_prd129(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-129'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd129_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd129_manifest_sources VALUES
  ('edge_backup_reports','archive_then_delete','audit'),
  ('edge_backup_statuses','delete','operational'),
  ('edge_restore_jobs','archive_then_delete','audit'),
  ('edge_restore_verifications','archive_then_delete','audit'),
  ('private_dr_drills','archive_then_delete','audit'),
  ('edge_backup_events','archive_then_delete','audit'),
  ('edge_backup_outbox','delete','operational');
INSERT INTO tenant_deletion_table_manifest(
  source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
  trigger_set_sha256,catalog_revision,created_at)
SELECT table_record.name,source.disposition,source.retention_class,
  metahub_sha256(table_record.sql),
  metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info(table_record.name) ORDER BY cid))),
  metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT trigger_record.name||char(31)||metahub_sha256(trigger_record.sql) AS record
    FROM sqlite_schema trigger_record WHERE trigger_record.type='trigger'
      AND trigger_record.tbl_name=table_record.name ORDER BY trigger_record.name)),'')),
  1,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM sqlite_schema table_record
JOIN prd129_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd129(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-129',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd129_manifest_sources ORDER BY source_table;
DROP TABLE prd129_manifest_sources;
DELETE FROM tenant_deletion_trigger_manifest;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT trigger_record.name,trigger_record.tbl_name,trigger_record.sql,
  metahub_sha256(trigger_record.sql)
FROM sqlite_schema trigger_record
JOIN tenant_deletion_table_manifest manifest ON manifest.source_table=trigger_record.tbl_name
WHERE trigger_record.type='trigger' ORDER BY trigger_record.tbl_name,trigger_record.name;
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd129_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd129
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd129_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd129
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd129_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd129
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd129_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd129_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_edge_backup_permission_extensions)=4
      AND (SELECT COUNT(*) FROM workspace_edge_backup_role_permissions)=11
      AND (SELECT COUNT(*) FROM platform_edge_backup_permission_extensions)=2
      AND (SELECT COUNT(*) FROM platform_edge_backup_role_permissions)=5
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd129)=7
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd129_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
