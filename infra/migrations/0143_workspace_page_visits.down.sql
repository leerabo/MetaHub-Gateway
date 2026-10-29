-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE workspace_page_visits_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO workspace_page_visits_down_guard
SELECT COUNT(*) FROM console_page_events WHERE workspace_session_id IS NOT NULL;
DROP TABLE workspace_page_visits_down_guard;
DROP TRIGGER trg_console_page_visit_owner_prd147;
DROP TRIGGER trg_console_page_visit_immutable_prd147;
ALTER TABLE console_page_events DROP COLUMN visit_request_sha256;
ALTER TABLE console_page_events DROP COLUMN workspace_session_id;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DELETE FROM tenant_deletion_trigger_manifest
WHERE trigger_name IN ('trg_console_page_visit_owner_prd147','trg_console_page_visit_immutable_prd147');
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
UPDATE tenant_deletion_table_manifest SET
  table_sql_sha256=metahub_sha256((SELECT sql FROM sqlite_schema WHERE name='console_page_events')),
  column_signature_sha256=metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info('console_page_events') ORDER BY cid))),
  trigger_set_sha256=metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT name||char(31)||metahub_sha256(sql) AS record FROM sqlite_schema
    WHERE type='trigger' AND tbl_name='console_page_events' ORDER BY name)),''))
WHERE source_table='console_page_events';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
COMMIT;
PRAGMA foreign_keys = ON;
