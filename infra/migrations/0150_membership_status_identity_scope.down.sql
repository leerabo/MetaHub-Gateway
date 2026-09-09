-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE membership_identity_rollback_guard(
  value INTEGER CONSTRAINT membership_identity_rollback_blocked CHECK(value=0));
INSERT INTO membership_identity_rollback_guard SELECT
  (SELECT COUNT(*) FROM workspace_memberships WHERE status='disabled')
  +(SELECT COUNT(*) FROM membership_events WHERE event_type='membership.disabled');
DROP TABLE membership_identity_rollback_guard;
DROP TRIGGER trg_users_identity_status_update_prd091;
CREATE TRIGGER trg_users_identity_status_update_prd091
AFTER UPDATE OF status ON users FOR EACH ROW WHEN NEW.status!=OLD.status
BEGIN
  UPDATE global_accounts
  SET status=CASE WHEN login_enabled=1 THEN NEW.status ELSE status END,
      session_revision=session_revision+1,updated_at=NEW.updated_at
  WHERE account_id=(SELECT account_id FROM account_identity_links
    WHERE source_type='legacy_user' AND legacy_tenant_id=NEW.tenant_id
      AND legacy_user_id=NEW.user_id);
  UPDATE global_sessions
  SET status='revoked',revoked_at=NEW.updated_at,revoked_reason='account_disabled',
      updated_at=NEW.updated_at
  WHERE account_id=(SELECT account_id FROM account_identity_links
    WHERE source_type='legacy_user' AND legacy_tenant_id=NEW.tenant_id
      AND legacy_user_id=NEW.user_id) AND status='active' AND NEW.status='disabled';
END;

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
WHERE type='trigger' AND name='trg_users_identity_status_update_prd091'
ON CONFLICT(trigger_name) DO UPDATE SET trigger_sql=excluded.trigger_sql,trigger_sha256=excluded.trigger_sha256;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
COMMIT;
PRAGMA foreign_keys = ON;
