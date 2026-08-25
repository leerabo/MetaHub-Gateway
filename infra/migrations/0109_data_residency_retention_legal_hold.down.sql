-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd117_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd117_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0109' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM legal_holds)
  + (SELECT COUNT(*) FROM deletion_schedules)
  + (SELECT COUNT(*) FROM residency_evidence)
  + (SELECT COUNT(*) FROM data_governance_commands)
  + (SELECT COUNT(*) FROM data_governance_events)
  + (SELECT COUNT(*) FROM data_governance_event_outbox)
  + (SELECT COUNT(*) FROM residency_policies WHERE status!='active' OR revision!=1
      OR generation!=1 OR reason_code!='initial_policy' OR updated_by_account_id IS NOT NULL)
  + (SELECT COUNT(*) FROM retention_policies WHERE status!='active' OR revision!=1
      OR generation!=1)
  + (SELECT COUNT(*) FROM data_governance_fences WHERE policy_generation!=1
      OR hold_generation!=0 OR deletion_generation!=0 OR revision!=1)
  + (SELECT COUNT(*) FROM workspace_gateway_placements WHERE governance_generation!=1)
  + (SELECT COUNT(*) FROM backup_sets WHERE governance_generation IS NOT NULL
      OR residency_group IS NOT NULL)
  + (SELECT COUNT(*) FROM platform_jobs WHERE type_code='data.retention.delete')
  + CASE WHEN (SELECT COUNT(*) FROM data_classifications)=
      (SELECT COUNT(*)*6 FROM workspace_regions WHERE status='active') THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM retention_policies)=
      (SELECT COUNT(*)*6 FROM workspace_regions WHERE status='active') THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd117_down_guard;

DROP TRIGGER trg_workspace_regions_governance_seed_prd117;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_extensions_insert_prd117;
DROP TRIGGER trg_tenant_deletion_manifest_extensions_update_prd117;
DROP TRIGGER trg_tenant_deletion_manifest_extensions_delete_prd117;
CREATE TEMP TABLE prd117_manifest_extension_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions;
DROP TABLE tenant_deletion_manifest_extensions;
DELETE FROM tenant_deletion_trigger_manifest WHERE source_table IN(
  SELECT source_table FROM prd117_manifest_extension_sources);
DELETE FROM tenant_deletion_table_manifest WHERE source_table IN(
  SELECT source_table FROM prd117_manifest_extension_sources);
DROP TABLE prd117_manifest_extension_sources;

DROP TRIGGER trg_data_governance_outbox_delete_prd117;
DROP TRIGGER trg_data_governance_outbox_transition_prd117;
DROP TRIGGER trg_data_governance_events_delete_prd117;
DROP TRIGGER trg_data_governance_events_update_prd117;
DROP INDEX idx_data_governance_outbox_delivery_prd117;
DROP TABLE data_governance_event_outbox;
DROP TABLE data_governance_events;

DROP TRIGGER trg_data_governance_commands_delete_prd117;
DROP TRIGGER trg_data_governance_commands_update_prd117;
DROP TABLE data_governance_commands;

DROP TRIGGER trg_residency_evidence_delete_prd117;
DROP TRIGGER trg_residency_evidence_update_prd117;
DROP INDEX idx_residency_evidence_platform_prd117;
DROP TABLE residency_evidence;

DROP TRIGGER trg_deletion_schedules_delete_prd117;
DROP TRIGGER trg_deletion_schedules_transition_prd117;
DROP INDEX idx_deletion_schedule_due_prd117;
DROP TABLE deletion_schedules;

DROP TRIGGER trg_legal_holds_delete_prd117;
DROP TRIGGER trg_legal_holds_transition_prd117;
DROP INDEX idx_legal_hold_predicate_prd117;
DROP INDEX uq_legal_hold_active_prd117;
DROP TABLE legal_holds;

DROP TRIGGER trg_retention_policies_delete_prd117;
DROP TRIGGER trg_retention_policies_transition_prd117;
DROP INDEX idx_retention_policy_due_prd117;
DROP INDEX uq_retention_policy_active_prd117;
DROP TABLE retention_policies;

DROP TRIGGER trg_residency_policies_delete_prd117;
DROP TRIGGER trg_residency_policies_transition_prd117;
DROP INDEX idx_residency_policy_owner_prd117;
DROP INDEX uq_residency_policy_active_prd117;
DROP TABLE residency_policies;

DROP TRIGGER trg_data_governance_fences_delete_prd117;
DROP TRIGGER trg_data_governance_fences_transition_prd117;
DROP TABLE data_governance_fences;

DROP TRIGGER trg_data_classifications_delete_prd117;
DROP TRIGGER trg_data_classifications_update_prd117;
DROP TABLE data_classifications;

DROP TRIGGER trg_platform_job_types_delete_prd106;
DELETE FROM platform_job_types WHERE type_code='data.retention.delete';
CREATE TRIGGER trg_platform_job_types_delete_prd106 BEFORE DELETE ON platform_job_types
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;

ALTER TABLE backup_sets DROP COLUMN residency_group;
ALTER TABLE backup_sets DROP COLUMN governance_generation;
ALTER TABLE workspace_gateway_placements DROP COLUMN governance_generation;

DROP TRIGGER trg_platform_data_governance_roles_delete_prd117;
DROP TRIGGER trg_platform_data_governance_roles_update_prd117;
DROP TRIGGER trg_platform_data_governance_roles_insert_prd117;
DROP TRIGGER trg_platform_data_governance_permissions_delete_prd117;
DROP TRIGGER trg_platform_data_governance_permissions_update_prd117;
DROP TRIGGER trg_platform_data_governance_permissions_insert_prd117;
DROP TABLE platform_data_governance_role_permissions;
DROP TABLE platform_data_governance_permission_extensions;

DROP TRIGGER trg_workspace_governance_roles_delete_prd117;
DROP TRIGGER trg_workspace_governance_roles_update_prd117;
DROP TRIGGER trg_workspace_governance_roles_insert_prd117;
DROP TRIGGER trg_workspace_governance_permissions_delete_prd117;
DROP TRIGGER trg_workspace_governance_permissions_update_prd117;
DROP TRIGGER trg_workspace_governance_permissions_insert_prd117;
DROP TABLE workspace_governance_role_permissions;
DROP TABLE workspace_governance_permission_extensions;

-- Restore the PRD-089 manifest to the exact post-0108 schema after every reverse DDL.
CREATE TEMP TABLE prd117_manifest_refresh AS
SELECT table_record.name AS source_table,metahub_sha256(table_record.sql) AS table_sql_sha256,
  metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info(table_record.name) ORDER BY cid))) AS column_signature_sha256,
  metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT trigger_record.name||char(31)||metahub_sha256(trigger_record.sql) AS record
    FROM sqlite_schema trigger_record WHERE trigger_record.type='trigger'
      AND trigger_record.tbl_name=table_record.name ORDER BY trigger_record.name)),''))
    AS trigger_set_sha256
FROM sqlite_schema table_record
JOIN tenant_deletion_table_manifest manifest ON manifest.source_table=table_record.name
WHERE table_record.type='table';
CREATE TEMP TABLE prd117_manifest_refresh_guard(
  issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd117_manifest_refresh_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM prd117_manifest_refresh)=
    (SELECT COUNT(*) FROM tenant_deletion_table_manifest) THEN 0 ELSE 1 END;
DROP TABLE prd117_manifest_refresh_guard;
UPDATE tenant_deletion_table_manifest
SET table_sql_sha256=(SELECT refreshed.table_sql_sha256 FROM prd117_manifest_refresh refreshed
      WHERE refreshed.source_table=tenant_deletion_table_manifest.source_table),
    column_signature_sha256=(SELECT refreshed.column_signature_sha256 FROM prd117_manifest_refresh refreshed
      WHERE refreshed.source_table=tenant_deletion_table_manifest.source_table),
    trigger_set_sha256=(SELECT refreshed.trigger_set_sha256 FROM prd117_manifest_refresh refreshed
      WHERE refreshed.source_table=tenant_deletion_table_manifest.source_table);
DROP TABLE prd117_manifest_refresh;

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

CREATE TEMP TABLE prd117_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd117_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'data_classifications','data_governance_fences','residency_policies','retention_policies',
      'legal_holds','deletion_schedules','residency_evidence','data_governance_commands',
      'data_governance_events','data_governance_event_outbox',
      'workspace_governance_permission_extensions','workspace_governance_role_permissions',
      'platform_data_governance_permission_extensions','platform_data_governance_role_permissions',
      'tenant_deletion_manifest_extensions'))=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('workspace_gateway_placements')
      WHERE name='governance_generation')=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('backup_sets')
      WHERE name IN('governance_generation','residency_group'))=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_job_types
      WHERE type_code='data.retention.delete')=0 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd117_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
