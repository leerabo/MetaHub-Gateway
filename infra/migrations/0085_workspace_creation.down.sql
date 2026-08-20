-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd093_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd093_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0085' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM workspaces WHERE creation_source='user_created' OR revision!=1)
  + (SELECT COUNT(*) FROM organization_profiles WHERE revision!=1)
  + (SELECT COUNT(*) FROM personal_profiles)
  + (SELECT COUNT(*) FROM workspace_memberships
      WHERE source!='legacy_backfill' OR revision!=1)
  + (SELECT COUNT(*) FROM workspace_creation_requests)
  + (SELECT COUNT(*) FROM workspace_tenant_creation_claims)
  + (SELECT COUNT(*) FROM workspace_user_projection_claims)
  + (SELECT COUNT(*) FROM workspace_events)
  + (SELECT COUNT(*) FROM workspace_event_outbox)
  + (SELECT COUNT(*) FROM workspace_environment_origins WHERE origin!='legacy_existing')
  + (SELECT COUNT(*) FROM workspace_provisioning_jobs
      WHERE generation!=1 OR attempt_count!=0)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd093_down_guard;

DROP TRIGGER trg_users_projection_claim_validate_prd093;
DROP TRIGGER trg_users_identity_insert_prd093;
DROP TRIGGER trg_users_identity_password_update_prd093;
DROP TRIGGER trg_tenants_workspace_backfill_prd093;
DROP TRIGGER trg_environments_workspace_origin_prd093;

CREATE TRIGGER trg_users_identity_insert_prd091
AFTER INSERT ON users FOR EACH ROW
BEGIN
  INSERT INTO global_accounts(
    account_id,email_normalized,phone_e164,display_name,status,login_enabled,
    credential_revision,session_revision,mfa_state,failed_login_count,locked_until,
    last_login_at,created_at,updated_at)
  VALUES(
    'gac_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    lower(trim(NEW.email)),NULL,NEW.display_name,
    CASE WHEN length(trim(NEW.email))!=length(CAST(trim(NEW.email) AS BLOB))
        OR length(trim(NEW.email)) NOT BETWEEN 3 AND 320
        OR instr(trim(NEW.email),'@')<=1
        OR instr(substr(trim(NEW.email),instr(trim(NEW.email),'@')+1),'@')>0
        OR instr(trim(NEW.email),' ')>0
        OR metahub_password_hash_algorithm(NEW.password_hash) IS NULL
        OR (SELECT COUNT(*) FROM users x
          WHERE lower(trim(x.email))=lower(trim(NEW.email)))!=1
      THEN 'migration_conflict' ELSE NEW.status END,
    CASE WHEN length(trim(NEW.email))=length(CAST(trim(NEW.email) AS BLOB))
        AND length(trim(NEW.email)) BETWEEN 3 AND 320
        AND instr(trim(NEW.email),'@')>1
        AND instr(substr(trim(NEW.email),instr(trim(NEW.email),'@')+1),'@')=0
        AND instr(trim(NEW.email),' ')=0
        AND metahub_password_hash_algorithm(NEW.password_hash) IS NOT NULL
        AND (SELECT COUNT(*) FROM users x
          WHERE lower(trim(x.email))=lower(trim(NEW.email)))=1
      THEN 1 ELSE 0 END,
    1,1,'not_enrolled',NEW.failed_login_count,NEW.locked_until,NEW.last_login_at,
    NEW.created_at,NEW.updated_at);

  INSERT INTO account_credentials(
    credential_id,account_id,credential_type,algorithm,secret_hash,status,version,
    created_at,updated_at,rotated_at)
  VALUES(
    'acr_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    'gac_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    'password',coalesce(metahub_password_hash_algorithm(NEW.password_hash),'unknown'),
    NEW.password_hash,'active',1,NEW.created_at,NEW.updated_at,NULL);

  INSERT INTO account_identity_links(
    identity_link_id,account_id,source_type,legacy_tenant_id,legacy_user_id,status,linked_at)
  VALUES(
    'ail_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    'gac_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    'legacy_user',NEW.tenant_id,NEW.user_id,'active',NEW.created_at);

  UPDATE global_accounts SET status='migration_conflict',login_enabled=0,updated_at=NEW.updated_at
  WHERE account_id IN(
    SELECT l.account_id FROM account_identity_links l JOIN users u
      ON u.tenant_id=l.legacy_tenant_id AND u.user_id=l.legacy_user_id
    WHERE lower(trim(u.email))=lower(trim(NEW.email))
  ) AND (SELECT COUNT(*) FROM users x
    WHERE lower(trim(x.email))=lower(trim(NEW.email)))>1;

  INSERT OR IGNORE INTO identity_migration_conflicts(
    conflict_id,account_id,identifier_fingerprint,reason_code,status,detected_at)
  SELECT 'icf_'||substr(metahub_sha256(l.legacy_tenant_id||char(31)||l.legacy_user_id),1,32),
    l.account_id,metahub_sha256(lower(trim(NEW.email))),'duplicate_identifier','open',
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM account_identity_links l JOIN users u
    ON u.tenant_id=l.legacy_tenant_id AND u.user_id=l.legacy_user_id
  WHERE lower(trim(u.email))=lower(trim(NEW.email))
    AND (SELECT COUNT(*) FROM users x
      WHERE lower(trim(x.email))=lower(trim(NEW.email)))>1;

  INSERT OR IGNORE INTO identity_migration_conflicts(
    conflict_id,account_id,identifier_fingerprint,reason_code,status,detected_at)
  SELECT 'icf_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    'gac_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    metahub_sha256(lower(trim(NEW.email))),
    CASE WHEN length(trim(NEW.email))!=length(CAST(trim(NEW.email) AS BLOB))
        THEN 'identifier_requires_claim'
      WHEN metahub_password_hash_algorithm(NEW.password_hash) IS NULL
        THEN 'credential_unrecognized'
      ELSE 'identifier_invalid' END,
    'open',strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE (SELECT status FROM global_accounts WHERE account_id=
    'gac_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32))
    ='migration_conflict';
END;

CREATE TRIGGER trg_users_identity_password_update_prd091
AFTER UPDATE OF password_hash ON users FOR EACH ROW WHEN NEW.password_hash!=OLD.password_hash
BEGIN
  UPDATE account_credentials
  SET secret_hash=NEW.password_hash,
      algorithm=coalesce(metahub_password_hash_algorithm(NEW.password_hash),'unknown'),
      version=version+1,updated_at=NEW.updated_at,rotated_at=NEW.updated_at
  WHERE account_id=(SELECT account_id FROM account_identity_links
    WHERE source_type='legacy_user' AND legacy_tenant_id=NEW.tenant_id
      AND legacy_user_id=NEW.user_id);
  UPDATE global_accounts
  SET credential_revision=credential_revision+1,session_revision=session_revision+1,
      updated_at=NEW.updated_at,
      status=CASE WHEN metahub_password_hash_algorithm(NEW.password_hash) IS NOT NULL
        THEN CASE WHEN login_enabled=1 THEN NEW.status ELSE status END
        ELSE 'migration_conflict' END,
      login_enabled=CASE WHEN metahub_password_hash_algorithm(NEW.password_hash) IS NOT NULL
        THEN login_enabled ELSE 0 END
  WHERE account_id=(SELECT account_id FROM account_identity_links
    WHERE source_type='legacy_user' AND legacy_tenant_id=NEW.tenant_id
      AND legacy_user_id=NEW.user_id);
  UPDATE global_sessions
  SET status='revoked',revoked_at=NEW.updated_at,revoked_reason='credential_rotated',
      updated_at=NEW.updated_at
  WHERE account_id=(SELECT account_id FROM account_identity_links
    WHERE source_type='legacy_user' AND legacy_tenant_id=NEW.tenant_id
      AND legacy_user_id=NEW.user_id) AND status='active';
END;

DROP TRIGGER trg_workspace_tenant_creation_claims_delete_prd093;
DROP TRIGGER trg_workspace_tenant_creation_claims_update_prd093;
DROP TABLE workspace_tenant_creation_claims;
DROP TRIGGER trg_workspace_creation_requests_delete_prd093;
DROP TRIGGER trg_workspace_creation_requests_update_prd093;
DROP TABLE workspace_creation_requests;
DROP TRIGGER trg_workspace_projection_claims_delete_prd093;
DROP TRIGGER trg_workspace_projection_claims_update_prd093;
DROP TABLE workspace_user_projection_claims;
DROP TABLE workspace_event_outbox;
DROP TRIGGER trg_workspace_events_delete_prd093;
DROP TRIGGER trg_workspace_events_update_prd093;
DROP TABLE workspace_events;
DROP TRIGGER trg_workspace_environment_origins_delete_prd093;
DROP TRIGGER trg_workspace_environment_origins_update_prd093;
DROP TABLE workspace_environment_origins;
DROP TABLE workspace_provisioning_jobs;
DROP TRIGGER trg_workspace_memberships_delete_prd093;
DROP TRIGGER trg_workspace_memberships_update_prd093;
DROP TABLE workspace_memberships;
DROP TABLE personal_profiles;
DROP TABLE organization_profiles;
DROP TRIGGER trg_workspace_regions_delete_prd093;
DROP TRIGGER trg_workspace_regions_update_prd093;
DROP TABLE workspace_regions;
DROP TRIGGER trg_workspace_tenant_bindings_delete_prd093;
DROP TRIGGER trg_workspace_tenant_bindings_update_prd093;
DROP TABLE workspace_tenant_bindings;
DROP TABLE workspaces;

CREATE TEMP TABLE prd093_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd093_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd093')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN(
        'trg_users_identity_insert_prd091','trg_users_identity_password_update_prd091'))=2
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd093_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
