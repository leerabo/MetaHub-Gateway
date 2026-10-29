-- Organization status is not global account status.
CREATE OR REPLACE FUNCTION fn_trg_users_identity_status_update_prd091() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status!=OLD.status AND NOT EXISTS(SELECT 1 FROM workspace_memberships m
      WHERE m.tenant_id=NEW.tenant_id AND m.legacy_user_id=NEW.user_id) THEN
    UPDATE global_accounts
    SET status=CASE WHEN login_enabled=1 THEN NEW.status ELSE status END,
        session_revision=session_revision+1,updated_at=NEW.updated_at
    WHERE account_id=(SELECT account_id FROM account_identity_links
      WHERE source_type='legacy_user' AND legacy_tenant_id=NEW.tenant_id
        AND legacy_user_id=NEW.user_id);
    UPDATE global_sessions
    SET status='revoked',revoked_at=NEW.updated_at,revoked_reason='account_disabled',updated_at=NEW.updated_at
    WHERE account_id=(SELECT account_id FROM account_identity_links
      WHERE source_type='legacy_user' AND legacy_tenant_id=NEW.tenant_id
        AND legacy_user_id=NEW.user_id) AND status='active' AND NEW.status='disabled';
  END IF;
  RETURN NEW;
END $$;

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
FROM pg_trigger WHERE tgrelid='users'::regclass AND tgname='trg_users_identity_status_update_prd091'
ON CONFLICT(trigger_name) DO UPDATE SET trigger_sql=excluded.trigger_sql,trigger_sha256=excluded.trigger_sha256;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();
