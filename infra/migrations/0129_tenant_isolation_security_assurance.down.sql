-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd137_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd137_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0129' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM security_assessments)
  + (SELECT COUNT(*) FROM assurance_runner_leases)
  + (SELECT COUNT(*) FROM isolation_test_runs)
  + (SELECT COUNT(*) FROM pen_test_findings)
  + (SELECT COUNT(*) FROM security_remediations)
  + (SELECT COUNT(*) FROM security_risk_acceptances)
  + (SELECT COUNT(*) FROM assurance_cleanup_journal)
  + (SELECT COUNT(*) FROM security_assurance_events)
  + (SELECT COUNT(*) FROM security_assurance_event_outbox)
  + (SELECT COUNT(*) FROM security_assurance_action_commands)
  + CASE WHEN (SELECT COUNT(*) FROM security_assurance_profiles)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM security_assurance_scenarios)=10 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd137_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd137_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd137_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd137_delete;
CREATE TEMP TABLE prd137_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd137;
DROP TABLE tenant_deletion_manifest_extensions_prd137;
DELETE FROM tenant_deletion_trigger_manifest;
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd137_manifest_sources);
DROP TABLE prd137_manifest_sources;

DROP TRIGGER trg_assurance_scheduler_delete_prd137;
DROP TRIGGER trg_assurance_scheduler_update_prd137;
DROP TABLE security_assurance_scheduler_state;
DROP TRIGGER trg_assurance_command_delete_prd137;
DROP TRIGGER trg_assurance_command_update_prd137;
DROP TRIGGER trg_assurance_command_owner_insert_prd137;
DROP TABLE security_assurance_action_commands;
DROP TRIGGER trg_assurance_outbox_delete_prd137;
DROP TRIGGER trg_assurance_outbox_update_prd137;
DROP INDEX idx_assurance_outbox_due_prd137;
DROP TABLE security_assurance_event_outbox;
DROP TRIGGER trg_assurance_event_delete_prd137;
DROP TRIGGER trg_assurance_event_update_prd137;
DROP TRIGGER trg_assurance_event_owner_insert_prd137;
DROP TABLE security_assurance_events;
DROP TRIGGER trg_assurance_cleanup_delete_prd137;
DROP TRIGGER trg_assurance_cleanup_update_prd137;
DROP TABLE assurance_cleanup_journal;
DROP TRIGGER trg_risk_acceptance_delete_prd137;
DROP TRIGGER trg_risk_acceptance_update_prd137;
DROP INDEX idx_risk_acceptance_owner_prd137;
DROP TABLE security_risk_acceptances;
DROP TRIGGER trg_security_remediation_delete_prd137;
DROP TRIGGER trg_security_remediation_update_prd137;
DROP TABLE security_remediations;
DROP TRIGGER trg_pen_test_finding_delete_prd137;
DROP TRIGGER trg_pen_test_finding_update_prd137;
DROP INDEX idx_pen_test_findings_owner_prd137;
DROP TABLE pen_test_findings;
DROP TRIGGER trg_isolation_run_delete_prd137;
DROP TRIGGER trg_isolation_run_update_prd137;
DROP TABLE isolation_test_runs;
DROP TRIGGER trg_assurance_lease_update_prd137;
DROP TABLE assurance_runner_leases;
DROP TRIGGER trg_security_assessment_delete_prd137;
DROP TRIGGER trg_security_assessment_update_prd137;
DROP INDEX idx_security_assessments_owner_prd137;
DROP TABLE security_assessments;
DROP TRIGGER trg_assurance_scenarios_delete_prd137;
DROP TRIGGER trg_assurance_scenarios_update_prd137;
DROP TRIGGER trg_assurance_scenarios_insert_prd137;
DROP TABLE security_assurance_scenarios;
DROP TRIGGER trg_assurance_profiles_delete_prd137;
DROP TRIGGER trg_assurance_profiles_update_prd137;
DROP TRIGGER trg_assurance_profiles_insert_prd137;
DROP TABLE security_assurance_profiles;
DROP TRIGGER trg_workspace_assurance_roles_delete_prd137;
DROP TRIGGER trg_workspace_assurance_roles_update_prd137;
DROP TRIGGER trg_workspace_assurance_roles_insert_prd137;
DROP TRIGGER trg_workspace_assurance_permissions_delete_prd137;
DROP TRIGGER trg_workspace_assurance_permissions_update_prd137;
DROP TRIGGER trg_workspace_assurance_permissions_insert_prd137;
DROP TABLE workspace_security_assurance_role_permissions;
DROP TABLE workspace_security_assurance_permission_extensions;
DROP TRIGGER trg_platform_assurance_roles_delete_prd137;
DROP TRIGGER trg_platform_assurance_roles_update_prd137;
DROP TRIGGER trg_platform_assurance_roles_insert_prd137;
DROP TRIGGER trg_platform_assurance_permissions_delete_prd137;
DROP TRIGGER trg_platform_assurance_permissions_update_prd137;
DROP TRIGGER trg_platform_assurance_permissions_insert_prd137;
DROP TABLE platform_security_assurance_role_permissions;
DROP TABLE platform_security_assurance_permission_extensions;

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

CREATE TEMP TABLE prd137_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd137_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_security_assurance_permission_extensions',
      'platform_security_assurance_role_permissions',
      'workspace_security_assurance_permission_extensions',
      'workspace_security_assurance_role_permissions','security_assurance_profiles',
      'security_assurance_scenarios','security_assessments','assurance_runner_leases',
      'isolation_test_runs','pen_test_findings','security_remediations',
      'security_risk_acceptances','assurance_cleanup_journal',
      'security_assurance_events','security_assurance_event_outbox',
      'security_assurance_action_commands','security_assurance_scheduler_state',
      'tenant_deletion_manifest_extensions_prd137'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd137_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
