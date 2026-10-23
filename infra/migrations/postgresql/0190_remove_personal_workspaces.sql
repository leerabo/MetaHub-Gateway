-- Runner owns the transaction. Restore every FK and user trigger before commit.
-- Capture the transitive FK closure before deleting any rows, including outboxes
-- that have no tenant/workspace columns of their own.
CREATE TEMP TABLE retired_personal_workspaces ON COMMIT DROP AS
SELECT w.workspace_id,b.tenant_id FROM workspaces w
LEFT JOIN workspace_tenant_bindings b ON b.workspace_id=w.workspace_id
WHERE w.kind='personal';

CREATE TEMP TABLE personal_removal_tables ON COMMIT DROP AS
SELECT c.oid,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname=current_schema() AND c.relkind='r';
CREATE TEMP TABLE personal_removal_rows(
  table_oid oid NOT NULL,row_tid tid NOT NULL,PRIMARY KEY(table_oid,row_tid)
) ON COMMIT DROP;
CREATE TEMP TABLE personal_removal_fks ON COMMIT DROP AS
SELECT c.oid,c.conrelid,c.confrelid,c.conname,c.conkey,c.confkey,
       pg_get_constraintdef(c.oid) definition
FROM pg_constraint c JOIN personal_removal_tables t ON t.oid=c.conrelid
WHERE c.contype='f';
CREATE TEMP TABLE personal_removal_triggers ON COMMIT DROP AS
SELECT t.tgrelid,t.tgname,t.tgenabled FROM pg_trigger t
JOIN personal_removal_tables p ON p.oid=t.tgrelid WHERE NOT t.tgisinternal;

DO $purge$
DECLARE item record; fk record; joins text; changed bigint; added bigint;
BEGIN
  -- A new database has no retired personal workspaces. Avoid dropping and
  -- rebuilding every FK when there are no rows to remove.
  IF NOT EXISTS(SELECT 1 FROM retired_personal_workspaces) THEN
    RETURN;
  END IF;
  -- Locks keep ctid row identities stable while the closure is collected.
  FOR item IN SELECT * FROM personal_removal_tables ORDER BY oid LOOP
    EXECUTE format('LOCK TABLE %s IN ACCESS EXCLUSIVE MODE',item.oid::regclass);
  END LOOP;
  FOR item IN SELECT t.oid,c.column_name
    FROM personal_removal_tables t JOIN information_schema.columns c
      ON c.table_schema=current_schema() AND c.table_name=t.relname
    WHERE c.column_name IN('workspace_id','tenant_id','legacy_tenant_id')
  LOOP
    EXECUTE format(
      'INSERT INTO personal_removal_rows SELECT %s,ctid FROM %s WHERE %I IN (SELECT %I FROM retired_personal_workspaces) ON CONFLICT DO NOTHING',
      item.oid,item.oid::regclass,item.column_name,
      CASE WHEN item.column_name='workspace_id' THEN 'workspace_id' ELSE 'tenant_id' END);
  END LOOP;
  LOOP
    changed:=0;
    FOR fk IN SELECT * FROM personal_removal_fks ORDER BY oid LOOP
      SELECT string_agg(format('child.%I=parent.%I',ca.attname,pa.attname),' AND ' ORDER BY pair.position)
      INTO joins FROM unnest(fk.conkey,fk.confkey) WITH ORDINALITY pair(child_key,parent_key,position)
      JOIN pg_attribute ca ON ca.attrelid=fk.conrelid AND ca.attnum=pair.child_key
      JOIN pg_attribute pa ON pa.attrelid=fk.confrelid AND pa.attnum=pair.parent_key;
      EXECUTE format(
        'INSERT INTO personal_removal_rows SELECT %s,child.ctid FROM %s child JOIN %s parent ON %s JOIN personal_removal_rows marked ON marked.table_oid=%s AND marked.row_tid=parent.ctid ON CONFLICT DO NOTHING',
        fk.conrelid,fk.conrelid::regclass,fk.confrelid::regclass,joins,fk.confrelid);
      GET DIAGNOSTICS added=ROW_COUNT;
      changed:=changed+added;
    END LOOP;
    EXIT WHEN changed=0;
  END LOOP;
  FOR item IN SELECT * FROM personal_removal_triggers LOOP
    EXECUTE format('ALTER TABLE %s DISABLE TRIGGER %I',item.tgrelid::regclass,item.tgname);
  END LOOP;
  FOR fk IN SELECT * FROM personal_removal_fks LOOP
    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',fk.conrelid::regclass,fk.conname);
  END LOOP;
  FOR item IN SELECT * FROM personal_removal_tables ORDER BY oid LOOP
    EXECUTE format('DELETE FROM %s target WHERE EXISTS(SELECT 1 FROM personal_removal_rows marked WHERE marked.table_oid=%s AND marked.row_tid=target.ctid)',item.oid::regclass,item.oid);
  END LOOP;
  -- Recreating constraints validates the remaining organization data.
  FOR fk IN SELECT * FROM personal_removal_fks LOOP
    EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I %s',fk.conrelid::regclass,fk.conname,fk.definition);
  END LOOP;
  FOR item IN SELECT * FROM personal_removal_triggers LOOP
    EXECUTE format('ALTER TABLE %s %s TRIGGER %I',item.tgrelid::regclass,
      CASE item.tgenabled WHEN 'D' THEN 'DISABLE' WHEN 'A' THEN 'ENABLE ALWAYS' WHEN 'R' THEN 'ENABLE REPLICA' ELSE 'ENABLE' END,item.tgname);
  END LOOP;
END $purge$;

DROP TABLE personal_profiles;
DROP INDEX IF EXISTS uq_workspaces_personal_creator_prd093;
-- Replace the old type checks by locating them by expression, not generated name.
DO $types$
DECLARE item record;
BEGIN
  FOR item IN SELECT conrelid,conname FROM pg_constraint
    WHERE contype='c' AND conrelid IN('workspaces'::regclass,'onboarding_progress'::regclass,'platform_customer_account_projections'::regclass)
      AND pg_get_constraintdef(oid) LIKE '%personal%'
  LOOP EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',item.conrelid::regclass,item.conname); END LOOP;
END $types$;
ALTER TABLE workspaces ADD CONSTRAINT workspace_organization_only CHECK(kind='organization');
ALTER TABLE onboarding_progress ADD CONSTRAINT onboarding_organization_only CHECK(workspace_kind='organization');
ALTER TABLE platform_customer_account_projections ADD CONSTRAINT customer_organization_only CHECK(workspace_kind='organization');
