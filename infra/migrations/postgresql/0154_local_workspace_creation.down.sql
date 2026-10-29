DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM local_workspace_creation_admissions) THEN
    RAISE EXCEPTION 'local_workspace_rollback_blocked';
  END IF;
END $$;
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
        AND EXISTS(SELECT 1 FROM account_verifications v WHERE v.account_id=a.account_id)
   ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='workspace_projection_claim_invalid'; END IF;
  RETURN NEW;
END $$;
DROP TABLE local_workspace_creation_admissions;
DROP FUNCTION validate_local_workspace_admission();
DROP FUNCTION reject_local_workspace_admission_change();
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089 ON tenant_deletion_trigger_manifest;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089 ON tenant_deletion_table_manifest;
DELETE FROM tenant_deletion_trigger_manifest WHERE source_table='local_workspace_creation_admissions';
DELETE FROM tenant_deletion_table_manifest WHERE source_table='local_workspace_creation_admissions';
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089 BEFORE DELETE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_delete_prd089();
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089 BEFORE DELETE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_delete_prd089();
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
UPDATE tenant_deletion_table_manifest SET trigger_set_sha256=metahub_sha256(coalesce((
  SELECT string_agg(tgname||chr(31)||metahub_sha256(pg_get_triggerdef(oid)||';'||chr(10)
    ||pg_get_functiondef(tgfoid)),chr(30) ORDER BY tgname)
  FROM pg_trigger WHERE tgrelid='users'::regclass AND NOT tgisinternal),'')) WHERE source_table='users';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
UPDATE tenant_deletion_trigger_manifest m SET
  trigger_sql=pg_get_triggerdef(t.oid)||';'||chr(10)||pg_get_functiondef(t.tgfoid),
  trigger_sha256=metahub_sha256(pg_get_triggerdef(t.oid)||';'||chr(10)||pg_get_functiondef(t.tgfoid))
FROM pg_trigger t WHERE t.tgrelid='users'::regclass AND t.tgname=m.trigger_name
  AND m.trigger_name='trg_users_projection_claim_validate_prd093';
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();
