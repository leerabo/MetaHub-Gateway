-- SQLite page visits remain at 0143; PostgreSQL uses its own ordered namespace.
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM console_page_events WHERE workspace_session_id IS NOT NULL) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='workspace_page_visits_rollback_has_facts';
  END IF;
END $$;
DROP TRIGGER trg_console_page_visit_owner_prd147 ON console_page_events;
DROP TRIGGER trg_console_page_visit_immutable_prd147 ON console_page_events;
DROP FUNCTION fn_console_page_visit_owner_prd147();
DROP FUNCTION fn_console_page_visit_immutable_prd147();
ALTER TABLE console_page_events DROP COLUMN visit_request_sha256;
ALTER TABLE console_page_events DROP COLUMN workspace_session_id;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089 ON tenant_deletion_trigger_manifest;
DELETE FROM tenant_deletion_trigger_manifest
WHERE trigger_name IN ('trg_console_page_visit_owner_prd147','trg_console_page_visit_immutable_prd147');
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089 BEFORE DELETE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_delete_prd089();
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute
  LEFT JOIN pg_attrdef default_value ON default_value.adrelid=attribute.attrelid
    AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='console_page_events'::regclass
    AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='console_page_events'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='console_page_events'::regclass AND NOT tgisinternal
)
UPDATE tenant_deletion_table_manifest SET
  table_sql_sha256=metahub_sha256('CREATE TABLE console_page_events ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')'),
  column_signature_sha256=metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum) FROM columns)),
  trigger_set_sha256=metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),''))
WHERE source_table='console_page_events';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();
