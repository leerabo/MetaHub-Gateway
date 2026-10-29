CREATE TABLE local_workspace_creation_admissions(
  workspace_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL REFERENCES global_accounts(account_id),
  registration_id TEXT NOT NULL REFERENCES local_registration_consents(registration_id),
  source_environment TEXT NOT NULL DEFAULT 'dev' CHECK(source_environment='dev'),
  policy_revision INTEGER NOT NULL CHECK(policy_revision>0),
  created_at TEXT NOT NULL,
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE FUNCTION validate_local_workspace_admission() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS(
    SELECT 1 FROM workspaces w
    JOIN local_registration_consents c ON c.account_id=w.created_by_account_id
    JOIN registration_policy p ON p.environment=NEW.source_environment
    JOIN workspace_tenant_creation_claims claim ON claim.workspace_id=w.workspace_id
    WHERE w.workspace_id=NEW.workspace_id AND w.created_by_account_id=NEW.account_id
      AND w.creation_source='user_created' AND w.status='active'
      AND c.registration_id=NEW.registration_id AND c.email_verified=0
      AND p.email_verification=0 AND p.revision=NEW.policy_revision
      AND claim.tenant_id=NEW.tenant_id AND claim.account_id=NEW.account_id
      AND claim.status='pending' AND claim.expires_at>=strftime('%Y-%m-%dT%H:%M:%SZ','now')
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='local_workspace_admission_invalid'; END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION reject_local_workspace_admission_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'local_workspace_admission_immutable'; END $$;
CREATE TRIGGER trg_local_workspace_admission_insert BEFORE INSERT ON local_workspace_creation_admissions
FOR EACH ROW EXECUTE FUNCTION validate_local_workspace_admission();
CREATE TRIGGER trg_local_workspace_admission_change BEFORE UPDATE OR DELETE ON local_workspace_creation_admissions
FOR EACH ROW EXECUTE FUNCTION reject_local_workspace_admission_change();

CREATE OR REPLACE FUNCTION fn_trg_users_projection_claim_validate_prd093() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM workspace_user_projection_claims c
      WHERE c.tenant_id=NEW.tenant_id AND c.legacy_user_id=NEW.user_id AND c.status='pending')
   AND NOT EXISTS(
      SELECT 1 FROM workspace_user_projection_claims c
      JOIN global_accounts a ON a.account_id=c.account_id
      JOIN account_credentials credential ON credential.account_id=a.account_id
        AND credential.credential_type='password' AND credential.status='active'
      WHERE c.tenant_id=NEW.tenant_id AND c.legacy_user_id=NEW.user_id
        AND c.status='pending' AND c.expires_at>=strftime('%Y-%m-%dT%H:%M:%SZ','now')
        AND c.credential_revision=a.credential_revision
        AND c.email_normalized=lower(trim(NEW.email)) AND credential.secret_hash=NEW.password_hash
        AND a.status='active' AND a.login_enabled=1
        AND (EXISTS(SELECT 1 FROM account_verifications v WHERE v.account_id=a.account_id)
          OR EXISTS(SELECT 1 FROM local_workspace_creation_admissions admission
            WHERE admission.workspace_id=c.workspace_id AND admission.tenant_id=c.tenant_id
              AND admission.account_id=a.account_id))
   ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='workspace_projection_claim_invalid'; END IF;
  RETURN NEW;
END $$;

WITH columns AS (
  SELECT a.attnum,a.attname,format_type(a.atttypid,a.atttypmod) AS type_name,
    a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default_sql
  FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
  WHERE a.attrelid='local_workspace_creation_admissions'::regclass
    AND a.attnum>0 AND NOT a.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='local_workspace_creation_admissions'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='local_workspace_creation_admissions'::regclass AND NOT tgisinternal
)
INSERT INTO tenant_deletion_table_manifest(
  source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
  trigger_set_sha256,catalog_revision,created_at)
SELECT 'local_workspace_creation_admissions','archive_then_delete','security',
  metahub_sha256('CREATE TABLE local_workspace_creation_admissions ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')'),
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum) FROM columns)),
  metahub_sha256((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30) ORDER BY tgname) FROM triggers)),
  1,CURRENT_TIMESTAMP;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT tgname,'local_workspace_creation_admissions',
  pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid),
  metahub_sha256(pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid))
FROM pg_trigger WHERE tgrelid='local_workspace_creation_admissions'::regclass AND NOT tgisinternal;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
UPDATE tenant_deletion_table_manifest SET trigger_set_sha256=metahub_sha256(coalesce((
  SELECT string_agg(tgname||chr(31)||metahub_sha256(pg_get_triggerdef(oid)||';'||chr(10)
    ||pg_get_functiondef(tgfoid)),chr(30) ORDER BY tgname)
  FROM pg_trigger WHERE tgrelid='users'::regclass AND NOT tgisinternal),'')) WHERE source_table='users';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT tgname,'users',pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid),
  metahub_sha256(pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid))
FROM pg_trigger WHERE tgrelid='users'::regclass AND tgname='trg_users_projection_claim_validate_prd093'
ON CONFLICT(trigger_name) DO UPDATE SET trigger_sql=excluded.trigger_sql,trigger_sha256=excluded.trigger_sha256;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();
