-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd095_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd095_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0087' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM account_mfa_factors)
  + (SELECT COUNT(*) FROM platform_operators)
  + (SELECT COUNT(*) FROM platform_sessions)
  + (SELECT COUNT(*) FROM support_grants)
  + (SELECT COUNT(*) FROM support_sessions)
  + (SELECT COUNT(*) FROM break_glass_sessions)
  + (SELECT COUNT(*) FROM platform_audit_logs)
  + (SELECT COUNT(*) FROM platform_event_outbox)
  + CASE WHEN (SELECT status FROM platform_bootstrap_state WHERE singleton_id=1)='open'
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd095_down_guard;

DROP TRIGGER trg_global_session_revoke_platform_prd095;
DROP TRIGGER trg_platform_operator_revoke_prd095;
DROP TRIGGER trg_platform_audit_delete_prd095;
DROP TRIGGER trg_platform_audit_update_prd095;
DROP TRIGGER trg_platform_catalog_state_delete_prd095;
DROP TRIGGER trg_platform_catalog_state_update_prd095;
DROP TRIGGER trg_platform_role_permissions_delete_prd095;
DROP TRIGGER trg_platform_role_permissions_update_prd095;
DROP TRIGGER trg_platform_role_permissions_insert_prd095;
DROP TRIGGER trg_platform_permissions_delete_prd095;
DROP TRIGGER trg_platform_permissions_update_prd095;
DROP TRIGGER trg_platform_permissions_insert_prd095;
DROP TRIGGER trg_platform_roles_delete_prd095;
DROP TRIGGER trg_platform_roles_update_prd095;
DROP TRIGGER trg_platform_roles_insert_prd095;
DROP TABLE platform_event_outbox;
DROP TABLE platform_audit_logs;
DROP TABLE break_glass_reviews;
DROP TABLE break_glass_sessions;
DROP TABLE support_sessions;
DROP TABLE support_grant_approvals;
DROP TABLE support_grants;
DROP TABLE platform_sessions;
DROP TABLE platform_bootstrap_state;
DROP TABLE platform_operator_role_bindings;
DROP TABLE platform_operators;
DROP TABLE platform_catalog_state;
DROP TABLE platform_role_permissions;
DROP TABLE platform_permissions;
DROP TABLE platform_roles;
DROP TABLE account_mfa_attempt_guards;
DROP TABLE account_mfa_factors;

CREATE TEMP TABLE prd095_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd095_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd095_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
