-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- Only a pristine compatibility projection can be contracted. Any security or conflict fact must
-- survive through a runtime feature rollback instead of being silently deleted by schema rollback.
CREATE TEMP TABLE prd091_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd091_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0083' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM global_sessions)
  + (SELECT COUNT(*) FROM identity_events)
  + (SELECT COUNT(*) FROM identity_event_outbox)
  + (SELECT COUNT(*) FROM identity_migration_conflicts)
  + (SELECT COUNT(*) FROM global_accounts
      WHERE credential_revision!=1 OR session_revision!=1 OR mfa_state!='not_enrolled'
        OR failed_login_count!=0 OR locked_until IS NOT NULL OR last_login_at IS NOT NULL)
  + CASE WHEN (SELECT COUNT(*) FROM global_accounts)!=(SELECT COUNT(*) FROM users)
      OR (SELECT COUNT(*) FROM account_credentials)!=(SELECT COUNT(*) FROM users)
      OR (SELECT COUNT(*) FROM account_identity_links)!=(SELECT COUNT(*) FROM users)
    THEN 1 ELSE 0 END
  + (SELECT COUNT(*) FROM users u LEFT JOIN account_identity_links l
      ON l.source_type='legacy_user' AND l.legacy_tenant_id=u.tenant_id
        AND l.legacy_user_id=u.user_id
    LEFT JOIN global_accounts a ON a.account_id=l.account_id
    LEFT JOIN account_credentials c ON c.account_id=a.account_id
      AND c.credential_type='password' AND c.status='active'
    WHERE l.identity_link_id IS NULL OR a.account_id IS NULL OR c.credential_id IS NULL
      OR a.email_normalized!=lower(trim(u.email)) OR a.display_name!=u.display_name
      OR a.status!=u.status OR a.login_enabled!=1 OR c.secret_hash!=u.password_hash
      OR c.version!=1)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd091_down_guard;

DROP TRIGGER trg_identity_conflicts_delete_prd091;
DROP TRIGGER trg_identity_conflicts_update_prd091;
DROP TRIGGER trg_identity_events_delete_prd091;
DROP TRIGGER trg_identity_events_update_prd091;
DROP TRIGGER trg_account_identity_links_delete_prd091;
DROP TRIGGER trg_account_identity_links_immutable_prd091;
DROP TRIGGER trg_users_identity_status_update_prd091;
DROP TRIGGER trg_users_identity_password_update_prd091;
DROP TRIGGER trg_users_identity_email_update_prd091;
DROP TRIGGER trg_users_identity_insert_prd091;

DROP TABLE identity_migration_conflicts;
DROP TABLE identity_event_outbox;
DROP TABLE identity_events;
DROP TABLE global_sessions;
DROP TABLE account_identity_links;
DROP TABLE account_credentials;
DROP TABLE global_accounts;

CREATE TEMP TABLE prd091_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd091_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd091')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd091_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
