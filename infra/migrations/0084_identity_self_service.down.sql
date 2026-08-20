-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd092_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd092_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0084' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM registration_intents)
  + (SELECT COUNT(*) FROM password_reset_intents)
  + (SELECT COUNT(*) FROM verification_challenges)
  + (SELECT COUNT(*) FROM account_verifications)
  + (SELECT COUNT(*) FROM consent_records)
  + (SELECT COUNT(*) FROM identity_abuse_buckets)
  + (SELECT COUNT(*) FROM invitation_identity_bindings)
  + (SELECT COUNT(*) FROM identity_lifecycle_events)
  + (SELECT COUNT(*) FROM identity_lifecycle_outbox)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd092_down_guard;

DROP TABLE identity_lifecycle_outbox;
DROP TRIGGER trg_identity_lifecycle_events_delete_prd092;
DROP TRIGGER trg_identity_lifecycle_events_update_prd092;
DROP TABLE identity_lifecycle_events;
DROP TRIGGER trg_invitation_identity_bindings_delete_prd092;
DROP TRIGGER trg_invitation_identity_bindings_update_prd092;
DROP TABLE invitation_identity_bindings;
DROP TABLE identity_abuse_buckets;
DROP TRIGGER trg_consent_records_delete_prd092;
DROP TRIGGER trg_consent_records_update_prd092;
DROP TABLE consent_records;
DROP TRIGGER trg_account_verifications_delete_prd092;
DROP TRIGGER trg_account_verifications_update_prd092;
DROP TABLE account_verifications;
DROP TABLE verification_challenges;
DROP TABLE password_reset_intents;
DROP TABLE registration_intents;

CREATE TEMP TABLE prd092_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd092_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd092')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd092_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
