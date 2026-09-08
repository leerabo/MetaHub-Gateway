BEGIN IMMEDIATE;

CREATE TEMP TABLE workspace_page_visits_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO workspace_page_visits_up_guard
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
  ORDER BY version DESC LIMIT 1)='0142' THEN 0 ELSE 1 END;
DROP TABLE workspace_page_visits_up_guard;

ALTER TABLE console_page_events ADD COLUMN workspace_session_id TEXT
  REFERENCES workspace_sessions(workspace_session_id);
ALTER TABLE console_page_events ADD COLUMN visit_request_sha256 TEXT CHECK(
  (workspace_session_id IS NULL AND visit_request_sha256 IS NULL)
  OR (workspace_session_id IS NOT NULL AND visit_request_sha256 IS NOT NULL
      AND length(visit_request_sha256)=64 AND visit_request_sha256 NOT GLOB '*[^0-9a-f]*'));

CREATE TRIGGER trg_console_page_visit_owner_prd147 BEFORE INSERT ON console_page_events
WHEN NEW.workspace_session_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NEW.session_id IS NOT NULL OR NOT EXISTS(
    SELECT 1 FROM workspace_sessions session
    JOIN workspace_memberships member ON member.workspace_membership_id=session.membership_id
    JOIN workspace_tenant_bindings workspace ON workspace.workspace_id=session.workspace_id
    JOIN users actor ON actor.user_id=member.legacy_user_id
    WHERE session.workspace_session_id=NEW.workspace_session_id
      AND session.tenant_id=NEW.tenant_id AND member.tenant_id=NEW.tenant_id
      AND workspace.tenant_id=NEW.tenant_id AND actor.tenant_id=NEW.tenant_id
      AND member.workspace_id=session.workspace_id AND member.account_id=session.account_id
      AND member.legacy_user_id=NEW.user_id
  ) THEN RAISE(ABORT,'workspace_page_visit_owner_invalid') END;
END;
CREATE TRIGGER trg_console_page_visit_immutable_prd147 BEFORE UPDATE ON console_page_events
WHEN OLD.workspace_session_id IS NOT NULL OR NEW.workspace_session_id IS NOT NULL
BEGIN SELECT RAISE(ABORT,'workspace_page_visit_immutable'); END;

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
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT name,tbl_name,sql,metahub_sha256(sql) FROM sqlite_schema
WHERE type='trigger' AND name IN ('trg_console_page_visit_owner_prd147','trg_console_page_visit_immutable_prd147');

COMMIT;
