-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd128_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 任何签发、刷新、ACK、lease 摘要或审计事实都会阻断静默回滚。
INSERT INTO prd128_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0120' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM edge_licenses)
  + (SELECT COUNT(*) FROM edge_signed_entitlements)
  + (SELECT COUNT(*) FROM edge_capacity_grants)
  + (SELECT COUNT(*) FROM edge_license_refresh_requests)
  + (SELECT COUNT(*) FROM edge_license_acknowledgements)
  + (SELECT COUNT(*) FROM edge_license_lease_reports)
  + (SELECT COUNT(*) FROM edge_license_audits)
  + (SELECT COUNT(*) FROM edge_license_outbox)
  + (SELECT COUNT(*) FROM edge_license_action_commands)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd128_down_guard;

-- 先解冻 tenant 清单并移除本 PRD 登记，再按依赖逆序删除表。
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd128_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd128_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd128_delete;
CREATE TEMP TABLE prd128_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd128;
DROP TABLE tenant_deletion_manifest_extensions_prd128;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd128_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd128_manifest_sources);
DROP TABLE prd128_manifest_sources;

DROP TRIGGER trg_edge_license_actions_delete_prd128;
DROP TRIGGER trg_edge_license_actions_update_prd128;
DROP TABLE edge_license_action_commands;
DROP TRIGGER trg_edge_license_outbox_delete_prd128;
DROP TRIGGER trg_edge_license_outbox_update_prd128;
DROP TABLE edge_license_outbox;
DROP TRIGGER trg_edge_license_audits_delete_prd128;
DROP TRIGGER trg_edge_license_audits_update_prd128;
DROP TABLE edge_license_audits;
DROP TRIGGER trg_edge_license_lease_reports_delete_prd128;
DROP TRIGGER trg_edge_license_lease_reports_update_prd128;
DROP TABLE edge_license_lease_reports;
DROP TRIGGER trg_edge_license_acks_delete_prd128;
DROP TRIGGER trg_edge_license_acks_update_prd128;
DROP TABLE edge_license_acknowledgements;
DROP TRIGGER trg_edge_license_refresh_delete_prd128;
DROP TRIGGER trg_edge_license_refresh_update_prd128;
DROP TABLE edge_license_refresh_requests;
DROP TRIGGER trg_edge_capacity_grants_delete_prd128;
DROP TRIGGER trg_edge_capacity_grants_update_prd128;
DROP TABLE edge_capacity_grants;
DROP TRIGGER trg_edge_signed_entitlements_delete_prd128;
DROP TRIGGER trg_edge_signed_entitlements_update_prd128;
DROP INDEX idx_edge_signed_entitlements_owner_prd128;
DROP TABLE edge_signed_entitlements;
DROP TRIGGER trg_edge_licenses_delete_prd128;
DROP TRIGGER trg_edge_licenses_update_prd128;
DROP TABLE edge_licenses;

DROP TRIGGER trg_platform_edge_license_roles_delete_prd128;
DROP TRIGGER trg_platform_edge_license_roles_update_prd128;
DROP TRIGGER trg_platform_edge_license_roles_insert_prd128;
DROP TRIGGER trg_platform_edge_license_permissions_delete_prd128;
DROP TRIGGER trg_platform_edge_license_permissions_update_prd128;
DROP TRIGGER trg_platform_edge_license_permissions_insert_prd128;
DROP TABLE platform_edge_license_role_permissions;
DROP TABLE platform_edge_license_permission_extensions;
DROP TRIGGER trg_workspace_edge_license_roles_delete_prd128;
DROP TRIGGER trg_workspace_edge_license_roles_update_prd128;
DROP TRIGGER trg_workspace_edge_license_roles_insert_prd128;
DROP TRIGGER trg_workspace_edge_license_permissions_delete_prd128;
DROP TRIGGER trg_workspace_edge_license_permissions_update_prd128;
DROP TRIGGER trg_workspace_edge_license_permissions_insert_prd128;
DROP TABLE workspace_edge_license_role_permissions;
DROP TABLE workspace_edge_license_permission_extensions;

-- 重建冻结清单触发器与 hash，确保 0119 基线恢复完整。
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

CREATE TEMP TABLE prd128_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd128_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_license_permission_extensions','workspace_edge_license_role_permissions',
      'platform_edge_license_permission_extensions','platform_edge_license_role_permissions',
      'edge_licenses','edge_signed_entitlements','edge_capacity_grants',
      'edge_license_refresh_requests','edge_license_acknowledgements',
      'edge_license_lease_reports','edge_license_audits','edge_license_outbox',
      'edge_license_action_commands','tenant_deletion_manifest_extensions_prd128'))=0
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd128_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
