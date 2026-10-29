-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd135_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd135_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0127' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM compliance_controls WHERE seeded!=1)
  + (SELECT COUNT(*) FROM compliance_control_mappings WHERE seeded!=1)
  + (SELECT COUNT(*) FROM compliance_evidence)
  + (SELECT COUNT(*) FROM compliance_exceptions)
  + (SELECT COUNT(*) FROM compliance_remediation_tasks)
  + (SELECT COUNT(*) FROM compliance_reports)
  + (SELECT COUNT(*) FROM compliance_report_items)
  + (SELECT COUNT(*) FROM compliance_events)
  + (SELECT COUNT(*) FROM compliance_event_outbox)
  + (SELECT COUNT(*) FROM compliance_action_commands)
  + (SELECT COUNT(*) FROM compliance_collector_replay_guards)
  + (SELECT COUNT(*) FROM service_accounts WHERE scope_template='compliance_collector')
  + (SELECT COUNT(*) FROM service_account_scopes WHERE permission_code='compliance.collect')
  + (SELECT COUNT(*) FROM machine_identity_audits
      WHERE permission_code='compliance.collect'
        OR action_code='machine.compliance_evidence_submitted'
        OR resource_type='compliance_evidence_collection')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd135_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd135_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd135_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd135_delete;
CREATE TEMP TABLE prd135_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd135;
DROP TABLE tenant_deletion_manifest_extensions_prd135;
DELETE FROM tenant_deletion_trigger_manifest;
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd135_manifest_sources);
DROP TABLE prd135_manifest_sources;

DROP TRIGGER trg_compliance_scheduler_delete_prd135;
DROP TRIGGER trg_compliance_scheduler_update_prd135;
DROP TABLE compliance_scheduler_state;
DROP TRIGGER trg_compliance_replay_delete_prd135;
DROP TRIGGER trg_compliance_replay_update_prd135;
DROP TABLE compliance_collector_replay_guards;
DROP TRIGGER trg_compliance_commands_delete_prd135;
DROP TRIGGER trg_compliance_commands_update_prd135;
DROP TABLE compliance_action_commands;
DROP TRIGGER trg_compliance_outbox_delete_prd135;
DROP TRIGGER trg_compliance_outbox_update_prd135;
DROP INDEX idx_compliance_outbox_due_prd135;
DROP TABLE compliance_event_outbox;
DROP TRIGGER trg_compliance_events_delete_prd135;
DROP TRIGGER trg_compliance_events_update_prd135;
DROP INDEX idx_compliance_events_owner_prd135;
DROP TABLE compliance_events;
DROP TRIGGER trg_compliance_report_items_delete_prd135;
DROP TRIGGER trg_compliance_report_items_update_prd135;
DROP TABLE compliance_report_items;
DROP TRIGGER trg_compliance_reports_delete_prd135;
DROP TRIGGER trg_compliance_reports_update_prd135;
DROP INDEX idx_compliance_reports_owner_prd135;
DROP TABLE compliance_reports;
DROP TRIGGER trg_compliance_remediation_delete_prd135;
DROP TRIGGER trg_compliance_remediation_update_prd135;
DROP TRIGGER trg_compliance_remediation_insert_prd135;
DROP INDEX idx_compliance_remediation_owner_prd135;
DROP TABLE compliance_remediation_tasks;
DROP TRIGGER trg_compliance_exceptions_delete_prd135;
DROP TRIGGER trg_compliance_exceptions_update_prd135;
DROP TRIGGER trg_compliance_exceptions_insert_prd135;
DROP INDEX idx_compliance_exceptions_owner_prd135;
DROP TABLE compliance_exceptions;
DROP TRIGGER trg_compliance_evidence_delete_prd135;
DROP TRIGGER trg_compliance_evidence_update_prd135;
DROP TRIGGER trg_compliance_evidence_insert_prd135;
DROP INDEX idx_compliance_evidence_freshness_prd135;
DROP INDEX idx_compliance_evidence_owner_prd135;
DROP TABLE compliance_evidence;
DROP TRIGGER trg_compliance_mappings_delete_prd135;
DROP TRIGGER trg_compliance_mappings_update_prd135;
DROP TABLE compliance_control_mappings;
DROP TRIGGER trg_compliance_controls_delete_prd135;
DROP TRIGGER trg_compliance_controls_update_prd135;
DROP TABLE compliance_controls;

DROP TRIGGER trg_machine_identity_audits_insert_prd118;
DROP TRIGGER trg_machine_identity_audits_update_prd118;
DROP TRIGGER trg_machine_identity_audits_delete_prd118;
DROP INDEX idx_machine_identity_audits_owner_prd118;
CREATE TABLE machine_identity_audits_prd135_down(
  machine_identity_audit_id TEXT PRIMARY KEY CHECK(length(machine_identity_audit_id)=36
    AND substr(machine_identity_audit_id,1,4)='mia_'
    AND substr(machine_identity_audit_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subject_type TEXT NOT NULL CHECK(subject_type IN('human','service_account')),
  subject_id TEXT NOT NULL CHECK(length(subject_id) BETWEEN 4 AND 80),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'service_account.created','oauth_client.created','oauth.token_issued',
    'service_account.credential_rotated','service_account.revoked',
    'machine.provider_listed','machine.request_log_listed')),
  resource_type TEXT NOT NULL CHECK(resource_type IN(
    'service_account','oauth_client','access_token_grant','provider_collection',
    'request_log_collection')),
  resource_id TEXT NOT NULL CHECK(length(resource_id) BETWEEN 1 AND 80),
  outcome TEXT NOT NULL CHECK(outcome IN('success','denied')),
  permission_code TEXT CHECK(permission_code IS NULL OR permission_code IN(
    'machine_identity.manage','provider.read','request_log.read')),
  resource_revision INTEGER NOT NULL CHECK(resource_revision>=0),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128
    AND trace_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
INSERT INTO machine_identity_audits_prd135_down SELECT * FROM machine_identity_audits;
CREATE TEMP TABLE prd135_machine_audit_down_copy_guard(
  issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd135_machine_audit_down_copy_guard(issue_count)
SELECT abs((SELECT COUNT(*) FROM machine_identity_audits)
  -(SELECT COUNT(*) FROM machine_identity_audits_prd135_down));
DROP TABLE prd135_machine_audit_down_copy_guard;
DROP TABLE machine_identity_audits;
ALTER TABLE machine_identity_audits_prd135_down RENAME TO machine_identity_audits;
CREATE INDEX idx_machine_identity_audits_owner_prd118
  ON machine_identity_audits(workspace_id,tenant_id,created_at,machine_identity_audit_id);
CREATE TRIGGER trg_machine_identity_audits_insert_prd118
BEFORE INSERT ON machine_identity_audits FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM workspace_tenant_bindings binding
      WHERE binding.workspace_id=NEW.workspace_id AND binding.tenant_id=NEW.tenant_id)
  OR (NEW.subject_type='human' AND NOT EXISTS(
      SELECT 1 FROM workspace_memberships member
      WHERE member.workspace_id=NEW.workspace_id AND member.tenant_id=NEW.tenant_id
        AND member.account_id=NEW.subject_id AND member.status='active'
        AND member.role_code!='service_account'))
  OR (NEW.subject_type='service_account' AND NOT EXISTS(
      SELECT 1 FROM service_accounts account
      WHERE account.workspace_id=NEW.workspace_id AND account.tenant_id=NEW.tenant_id
        AND account.service_account_id=NEW.subject_id))
  OR NOT(
    (NEW.subject_type='human' AND NEW.outcome='success'
      AND NEW.action_code='service_account.created'
      AND NEW.resource_type='service_account' AND EXISTS(
        SELECT 1 FROM service_accounts account
        WHERE account.workspace_id=NEW.workspace_id AND account.tenant_id=NEW.tenant_id
          AND account.service_account_id=NEW.resource_id
          AND account.revision=NEW.resource_revision))
    OR(NEW.subject_type='human' AND NEW.outcome='success'
      AND NEW.action_code='oauth_client.created'
      AND NEW.resource_type='oauth_client' AND EXISTS(
        SELECT 1 FROM oauth_clients client
        WHERE client.workspace_id=NEW.workspace_id AND client.tenant_id=NEW.tenant_id
          AND client.oauth_client_id=NEW.resource_id
          AND client.revision=NEW.resource_revision))
    OR(NEW.subject_type='human' AND NEW.outcome='success'
      AND NEW.action_code IN(
        'service_account.credential_rotated','service_account.revoked')
      AND NEW.resource_type='service_account' AND EXISTS(
        SELECT 1 FROM service_accounts account
        WHERE account.workspace_id=NEW.workspace_id AND account.tenant_id=NEW.tenant_id
          AND account.service_account_id=NEW.resource_id))
    OR(NEW.subject_type='service_account' AND NEW.outcome='success'
      AND NEW.action_code='oauth.token_issued'
      AND NEW.resource_type='access_token_grant' AND EXISTS(
        SELECT 1 FROM access_token_grants grant_record
        WHERE grant_record.workspace_id=NEW.workspace_id
          AND grant_record.tenant_id=NEW.tenant_id
          AND grant_record.service_account_id=NEW.subject_id
          AND grant_record.access_token_grant_id=NEW.resource_id))
    OR(NEW.subject_type='service_account'
      AND NEW.action_code='machine.provider_listed'
      AND NEW.resource_type='provider_collection' AND NEW.resource_id='/providers')
    OR(NEW.subject_type='service_account'
      AND NEW.action_code='machine.request_log_listed'
      AND NEW.resource_type='request_log_collection'
      AND NEW.resource_id='/request-logs'))
BEGIN SELECT RAISE(ABORT,'machine_identity_audit_owner_invalid'); END;
CREATE TRIGGER trg_machine_identity_audits_update_prd118
BEFORE UPDATE ON machine_identity_audits
BEGIN SELECT RAISE(ABORT,'machine_identity_audit_immutable'); END;
CREATE TRIGGER trg_machine_identity_audits_delete_prd118
BEFORE DELETE ON machine_identity_audits
BEGIN SELECT RAISE(ABORT,'machine_identity_audit_immutable'); END;

DROP TRIGGER trg_service_accounts_update_prd118;
DROP TRIGGER trg_service_accounts_delete_prd118;
DROP INDEX idx_service_accounts_owner_prd118;
ALTER TABLE service_accounts RENAME TO service_accounts_prd135;
CREATE TABLE service_accounts(
  service_account_id TEXT PRIMARY KEY CHECK(length(service_account_id)=36
    AND substr(service_account_id,1,4)='svc_'
    AND substr(service_account_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL CHECK(length(name) BETWEEN 2 AND 80
    AND name=lower(name) AND name NOT GLOB '*[^a-z0-9_-]*'),
  display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 2 AND 120),
  status TEXT NOT NULL CHECK(status IN('active','disabled','revoked')),
  scope_template TEXT NOT NULL CHECK(scope_template IN(
    'provider_reader','observability_reader')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1),
  constraint_sha256 TEXT NOT NULL CHECK(length(constraint_sha256)=64
    AND constraint_sha256 NOT GLOB '*[^0-9a-f]*'),
  constraint_generation INTEGER NOT NULL CHECK(constraint_generation>=1),
  ip_allowlist_json TEXT NOT NULL CHECK(json_valid(ip_allowlist_json)
    AND json_type(ip_allowlist_json)='array' AND length(ip_allowlist_json)<=2048),
  mtls_fingerprint_sha256 TEXT CHECK(mtls_fingerprint_sha256 IS NULL
    OR(length(mtls_fingerprint_sha256)=64
      AND mtls_fingerprint_sha256 NOT GLOB '*[^0-9a-f]*')),
  token_ttl_seconds INTEGER NOT NULL CHECK(token_ttl_seconds BETWEEN 60 AND 3600),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_account_id TEXT NOT NULL,
  updated_by_account_id TEXT NOT NULL,
  last_used_at TEXT CHECK(last_used_at IS NULL OR unixepoch(last_used_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(created_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(updated_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,name),
  UNIQUE(workspace_id,tenant_id,service_account_id),
  CHECK((status='revoked' AND revoked_at IS NOT NULL)
    OR(status!='revoked' AND revoked_at IS NULL))
);
INSERT INTO service_accounts SELECT * FROM service_accounts_prd135;
DROP TABLE service_accounts_prd135;
CREATE INDEX idx_service_accounts_owner_prd118
  ON service_accounts(workspace_id,tenant_id,status,created_at,service_account_id);
CREATE TRIGGER trg_service_accounts_update_prd118
BEFORE UPDATE ON service_accounts FOR EACH ROW
WHEN NEW.service_account_id!=OLD.service_account_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.name!=OLD.name OR NEW.display_name!=OLD.display_name
  OR NEW.scope_template!=OLD.scope_template OR NEW.catalog_revision!=OLD.catalog_revision
  OR NEW.constraint_sha256!=OLD.constraint_sha256
  OR NEW.ip_allowlist_json!=OLD.ip_allowlist_json
  OR NEW.mtls_fingerprint_sha256 IS NOT OLD.mtls_fingerprint_sha256
  OR NEW.token_ttl_seconds!=OLD.token_ttl_seconds
  OR NEW.created_by_account_id!=OLD.created_by_account_id OR NEW.created_at!=OLD.created_at
  OR NEW.revision NOT IN(OLD.revision,OLD.revision+1)
  OR NEW.constraint_generation NOT IN(OLD.constraint_generation,OLD.constraint_generation+1)
  OR (NEW.revision=OLD.revision AND NEW.constraint_generation!=OLD.constraint_generation)
  OR (NEW.revision=OLD.revision+1 AND NEW.constraint_generation!=OLD.constraint_generation+1)
  OR (NEW.revision=OLD.revision AND (NEW.status!=OLD.status
      OR NEW.updated_by_account_id!=OLD.updated_by_account_id
      OR NEW.revoked_at IS NOT OLD.revoked_at))
  OR (NEW.revision=OLD.revision+1 AND NOT(
      OLD.status IN('active','disabled') AND NEW.status='revoked'
      AND OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL))
  OR (NEW.last_used_at IS NOT NULL AND OLD.last_used_at IS NOT NULL
      AND NEW.last_used_at<OLD.last_used_at)
  OR NEW.updated_at<OLD.updated_at
BEGIN SELECT RAISE(ABORT,'service_account_transition_invalid'); END;
CREATE TRIGGER trg_service_accounts_delete_prd118
BEFORE DELETE ON service_accounts
BEGIN SELECT RAISE(ABORT,'service_account_immutable'); END;

DROP TRIGGER trg_machine_permission_catalog_insert_prd118;
DROP TRIGGER trg_machine_permission_catalog_update_prd118;
DROP TRIGGER trg_machine_permission_catalog_delete_prd118;
ALTER TABLE machine_permission_catalog RENAME TO machine_permission_catalog_prd135;
CREATE TABLE machine_permission_catalog(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'provider.read','request_log.read')),
  scope_template TEXT NOT NULL CHECK(scope_template IN(
    'provider_reader','observability_reader')),
  http_method TEXT NOT NULL CHECK(http_method='GET'),
  route_template TEXT NOT NULL CHECK(route_template IN('/providers','/request-logs')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1),
  status TEXT NOT NULL CHECK(status='active'),
  UNIQUE(http_method,route_template),
  UNIQUE(scope_template,permission_code)
);
INSERT INTO machine_permission_catalog
SELECT * FROM machine_permission_catalog_prd135 WHERE permission_code!='compliance.collect';
DROP TABLE machine_permission_catalog_prd135;
CREATE TRIGGER trg_machine_permission_catalog_insert_prd118
BEFORE INSERT ON machine_permission_catalog
BEGIN SELECT RAISE(ABORT,'machine_permission_catalog_immutable'); END;
CREATE TRIGGER trg_machine_permission_catalog_update_prd118
BEFORE UPDATE ON machine_permission_catalog
BEGIN SELECT RAISE(ABORT,'machine_permission_catalog_immutable'); END;
CREATE TRIGGER trg_machine_permission_catalog_delete_prd118
BEFORE DELETE ON machine_permission_catalog
BEGIN SELECT RAISE(ABORT,'machine_permission_catalog_immutable'); END;

DROP TRIGGER trg_platform_compliance_roles_delete_prd135;
DROP TRIGGER trg_platform_compliance_roles_update_prd135;
DROP TRIGGER trg_platform_compliance_roles_insert_prd135;
DROP TRIGGER trg_platform_compliance_permissions_delete_prd135;
DROP TRIGGER trg_platform_compliance_permissions_update_prd135;
DROP TRIGGER trg_platform_compliance_permissions_insert_prd135;
DROP TABLE platform_compliance_role_permissions;
DROP TABLE platform_compliance_permission_extensions;
DROP TRIGGER trg_workspace_compliance_roles_delete_prd135;
DROP TRIGGER trg_workspace_compliance_roles_update_prd135;
DROP TRIGGER trg_workspace_compliance_roles_insert_prd135;
DROP TRIGGER trg_workspace_compliance_permissions_delete_prd135;
DROP TRIGGER trg_workspace_compliance_permissions_update_prd135;
DROP TRIGGER trg_workspace_compliance_permissions_insert_prd135;
DROP TABLE workspace_compliance_role_permissions;
DROP TABLE workspace_compliance_permission_extensions;

UPDATE tenant_deletion_table_manifest
SET table_sql_sha256=(SELECT metahub_sha256(sql) FROM sqlite_schema
      WHERE type='table' AND name='service_accounts'),
    column_signature_sha256=metahub_sha256((SELECT group_concat(record,char(30)) FROM(
      SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
        ||coalesce(dflt_value,'')||char(31)||pk AS record
      FROM pragma_table_info('service_accounts') ORDER BY cid))),
    trigger_set_sha256=metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
      SELECT trigger_record.name||char(31)||metahub_sha256(trigger_record.sql) AS record
      FROM sqlite_schema trigger_record WHERE trigger_record.type='trigger'
        AND trigger_record.tbl_name='service_accounts' ORDER BY trigger_record.name)),''))
WHERE source_table='service_accounts';

UPDATE tenant_deletion_table_manifest
SET table_sql_sha256=(SELECT metahub_sha256(sql) FROM sqlite_schema
      WHERE type='table' AND name='machine_identity_audits'),
    column_signature_sha256=metahub_sha256((SELECT group_concat(record,char(30)) FROM(
      SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
        ||coalesce(dflt_value,'')||char(31)||pk AS record
      FROM pragma_table_info('machine_identity_audits') ORDER BY cid))),
    trigger_set_sha256=metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
      SELECT trigger_record.name||char(31)||metahub_sha256(trigger_record.sql) AS record
      FROM sqlite_schema trigger_record WHERE trigger_record.type='trigger'
        AND trigger_record.tbl_name='machine_identity_audits'
      ORDER BY trigger_record.name)),''))
WHERE source_table='machine_identity_audits';

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

CREATE TEMP TABLE prd135_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd135_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_compliance_permission_extensions','workspace_compliance_role_permissions',
      'platform_compliance_permission_extensions','platform_compliance_role_permissions',
      'compliance_controls','compliance_control_mappings','compliance_evidence',
      'compliance_exceptions','compliance_remediation_tasks','compliance_reports',
      'compliance_report_items','compliance_events','compliance_event_outbox',
      'compliance_action_commands','compliance_collector_replay_guards',
      'compliance_scheduler_state','tenant_deletion_manifest_extensions_prd135'))=0
      AND (SELECT COUNT(*) FROM machine_permission_catalog)=2 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd135_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
