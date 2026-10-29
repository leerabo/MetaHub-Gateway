-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd123_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd123_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0115' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM edge_secret_references)
  + (SELECT COUNT(*) FROM edge_secret_sync_batches)
  + (SELECT COUNT(*) FROM edge_secret_events)
  + (SELECT COUNT(*) FROM edge_secret_sync_receipts)
  + (SELECT COUNT(*) FROM edge_secret_event_outbox)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd123_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd123_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd123_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd123_delete;
CREATE TEMP TABLE prd123_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd123;
DROP TABLE tenant_deletion_manifest_extensions_prd123;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd123_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd123_manifest_sources);
DROP TABLE prd123_manifest_sources;

DROP TRIGGER trg_edge_secret_outbox_delete_prd123;
DROP TRIGGER trg_edge_secret_outbox_update_prd123;
DROP INDEX idx_edge_secret_outbox_delivery_prd123;
DROP TABLE edge_secret_event_outbox;
DROP TRIGGER trg_edge_secret_sync_receipts_delete_prd123;
DROP TRIGGER trg_edge_secret_sync_receipts_update_prd123;
DROP TABLE edge_secret_sync_receipts;
DROP TRIGGER trg_edge_secret_events_delete_prd123;
DROP TRIGGER trg_edge_secret_events_update_prd123;
DROP INDEX idx_edge_secret_events_owner_prd123;
DROP TABLE edge_secret_events;
DROP TRIGGER trg_edge_secret_references_delete_prd123;
DROP TRIGGER trg_edge_secret_references_update_prd123;
DROP INDEX idx_edge_secret_references_owner_prd123;
DROP TABLE edge_secret_references;
DROP TRIGGER trg_edge_secret_sync_batches_delete_prd123;
DROP TRIGGER trg_edge_secret_sync_batches_update_prd123;
DROP INDEX idx_edge_secret_sync_batches_owner_prd123;
DROP TABLE edge_secret_sync_batches;
DROP TRIGGER trg_workspace_edge_secret_roles_delete_prd123;
DROP TRIGGER trg_workspace_edge_secret_roles_update_prd123;
DROP TRIGGER trg_workspace_edge_secret_roles_insert_prd123;
DROP TRIGGER trg_workspace_edge_secret_permissions_delete_prd123;
DROP TRIGGER trg_workspace_edge_secret_permissions_update_prd123;
DROP TRIGGER trg_workspace_edge_secret_permissions_insert_prd123;
DROP TABLE workspace_edge_secret_role_permissions;
DROP TABLE workspace_edge_secret_permission_extensions;

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

CREATE TEMP TABLE prd123_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd123_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_secret_permission_extensions','workspace_edge_secret_role_permissions',
      'edge_secret_references','edge_secret_sync_batches','edge_secret_events',
      'edge_secret_sync_receipts','edge_secret_event_outbox',
      'tenant_deletion_manifest_extensions_prd123'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd123_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
