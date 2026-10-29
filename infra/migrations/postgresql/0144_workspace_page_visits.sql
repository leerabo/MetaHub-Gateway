-- PostgreSQL 0143 repairs invitation authorization before real Workspace bootstrap.
ALTER TABLE console_page_events ADD COLUMN workspace_session_id TEXT
  REFERENCES workspace_sessions(workspace_session_id);
ALTER TABLE console_page_events ADD COLUMN visit_request_sha256 TEXT CHECK(
  (workspace_session_id IS NULL AND visit_request_sha256 IS NULL)
  OR (workspace_session_id IS NOT NULL AND visit_request_sha256 IS NOT NULL
      AND visit_request_sha256 ~ '^[0-9a-f]{64}$'));

CREATE FUNCTION fn_console_page_visit_owner_prd147() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.workspace_session_id IS NOT NULL AND (NEW.session_id IS NOT NULL OR NOT EXISTS(
    SELECT 1 FROM workspace_sessions session
    JOIN workspace_memberships member ON member.workspace_membership_id=session.membership_id
    JOIN workspace_tenant_bindings workspace ON workspace.workspace_id=session.workspace_id
    JOIN users actor ON actor.user_id=member.legacy_user_id
    WHERE session.workspace_session_id=NEW.workspace_session_id
      AND session.tenant_id=NEW.tenant_id AND member.tenant_id=NEW.tenant_id
      AND workspace.tenant_id=NEW.tenant_id AND actor.tenant_id=NEW.tenant_id
      AND member.workspace_id=session.workspace_id AND member.account_id=session.account_id
      AND member.legacy_user_id=NEW.user_id
  )) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='workspace_page_visit_owner_invalid';
  END IF;
  RETURN NEW;
END;
$$;
CREATE FUNCTION fn_console_page_visit_immutable_prd147() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.workspace_session_id IS NOT NULL OR NEW.workspace_session_id IS NOT NULL THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='workspace_page_visit_immutable';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER trg_console_page_visit_owner_prd147 BEFORE INSERT ON console_page_events
FOR EACH ROW EXECUTE FUNCTION fn_console_page_visit_owner_prd147();
CREATE TRIGGER trg_console_page_visit_immutable_prd147 BEFORE UPDATE ON console_page_events
FOR EACH ROW EXECUTE FUNCTION fn_console_page_visit_immutable_prd147();

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
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT tgname,'console_page_events',pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid),
  metahub_sha256(pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid))
FROM pg_trigger WHERE tgrelid='console_page_events'::regclass
  AND tgname IN ('trg_console_page_visit_owner_prd147','trg_console_page_visit_immutable_prd147');
