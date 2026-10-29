BEGIN IMMEDIATE;
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
CREATE TRIGGER trg_local_workspace_admission_insert BEFORE INSERT ON local_workspace_creation_admissions
WHEN NOT EXISTS(
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
)
BEGIN SELECT RAISE(ABORT,'local_workspace_admission_invalid'); END;
CREATE TRIGGER trg_local_workspace_admission_update BEFORE UPDATE ON local_workspace_creation_admissions
BEGIN SELECT RAISE(ABORT,'local_workspace_admission_immutable'); END;
CREATE TRIGGER trg_local_workspace_admission_delete BEFORE DELETE ON local_workspace_creation_admissions
BEGIN SELECT RAISE(ABORT,'local_workspace_admission_immutable'); END;

DROP TRIGGER trg_users_projection_claim_validate_prd093;
CREATE TRIGGER trg_users_projection_claim_validate_prd093
BEFORE INSERT ON users FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM workspace_user_projection_claims c
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
 )
BEGIN SELECT RAISE(ABORT,'workspace_projection_claim_invalid'); END;

INSERT INTO tenant_deletion_table_manifest(
  source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
  trigger_set_sha256,catalog_revision,created_at)
SELECT t.name,'archive_then_delete','security',metahub_sha256(t.sql),
  metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info(t.name) ORDER BY cid))),
  metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT name||char(31)||metahub_sha256(sql) AS record FROM sqlite_schema
    WHERE type='trigger' AND tbl_name=t.name ORDER BY name)),'')),
  1,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM sqlite_schema t WHERE type='table' AND name='local_workspace_creation_admissions';
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT name,tbl_name,sql,metahub_sha256(sql) FROM sqlite_schema
WHERE type='trigger' AND tbl_name='local_workspace_creation_admissions';
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
UPDATE tenant_deletion_table_manifest SET trigger_set_sha256=metahub_sha256(
  coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT name||char(31)||metahub_sha256(sql) AS record FROM sqlite_schema
    WHERE type='trigger' AND tbl_name='users' ORDER BY name)),'')) WHERE source_table='users';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT name,tbl_name,sql,metahub_sha256(sql) FROM sqlite_schema
WHERE type='trigger' AND name='trg_users_projection_claim_validate_prd093'
ON CONFLICT(trigger_name) DO UPDATE SET trigger_sql=excluded.trigger_sql,trigger_sha256=excluded.trigger_sha256;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
COMMIT;
