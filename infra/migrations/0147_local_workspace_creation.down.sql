-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE local_workspace_rollback_guard(value INTEGER CHECK(value=0));
INSERT INTO local_workspace_rollback_guard SELECT COUNT(*) FROM local_workspace_creation_admissions;
DROP TABLE local_workspace_rollback_guard;
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
      AND EXISTS(SELECT 1 FROM account_verifications v WHERE v.account_id=a.account_id)
 )
BEGIN SELECT RAISE(ABORT,'workspace_projection_claim_invalid'); END;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DELETE FROM tenant_deletion_trigger_manifest WHERE source_table='local_workspace_creation_admissions';
DELETE FROM tenant_deletion_table_manifest WHERE source_table='local_workspace_creation_admissions';
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
DROP TABLE local_workspace_creation_admissions;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
UPDATE tenant_deletion_table_manifest SET trigger_set_sha256=metahub_sha256(
  coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT name||char(31)||metahub_sha256(sql) AS record FROM sqlite_schema
    WHERE type='trigger' AND tbl_name='users' ORDER BY name)),'')) WHERE source_table='users';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
UPDATE tenant_deletion_trigger_manifest SET
  trigger_sql=(SELECT sql FROM sqlite_schema WHERE name=trigger_name),
  trigger_sha256=metahub_sha256((SELECT sql FROM sqlite_schema WHERE name=trigger_name))
WHERE trigger_name='trg_users_projection_claim_validate_prd093';
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
COMMIT;
PRAGMA foreign_keys = ON;
