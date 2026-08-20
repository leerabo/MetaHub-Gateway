PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- PRD-091 only expands the completed 0082 schema. Rebuilding users or sessions would break
-- the many tenant-qualified facts that still reference those legacy compatibility projections.
CREATE TEMP TABLE prd091_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd091_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0082' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd091')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd091_up_guard;

CREATE TABLE global_accounts(
  account_id TEXT PRIMARY KEY CHECK(length(account_id)=36
    AND substr(account_id,1,4)='gac_'
    AND substr(account_id,5) NOT GLOB '*[^0-9a-f]*'),
  email_normalized TEXT NOT NULL CHECK(length(email_normalized) BETWEEN 3 AND 320),
  phone_e164 TEXT CHECK(phone_e164 IS NULL OR(length(phone_e164) BETWEEN 8 AND 16
    AND substr(phone_e164,1,1)='+' AND substr(phone_e164,2) NOT GLOB '*[^0-9]*')),
  display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 120),
  status TEXT NOT NULL CHECK(status IN('active','disabled','locked','migration_conflict')),
  login_enabled INTEGER NOT NULL CHECK(login_enabled IN(0,1)),
  credential_revision INTEGER NOT NULL DEFAULT 1 CHECK(credential_revision>=1),
  session_revision INTEGER NOT NULL DEFAULT 1 CHECK(session_revision>=1),
  mfa_state TEXT NOT NULL DEFAULT 'not_enrolled' CHECK(mfa_state='not_enrolled'),
  failed_login_count INTEGER NOT NULL DEFAULT 0 CHECK(failed_login_count>=0),
  locked_until TEXT CHECK(locked_until IS NULL OR unixepoch(locked_until) IS NOT NULL),
  last_login_at TEXT CHECK(last_login_at IS NULL OR unixepoch(last_login_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK(status!='migration_conflict' OR login_enabled=0),
  CHECK(status!='locked' OR locked_until IS NOT NULL)
);
CREATE UNIQUE INDEX uq_global_accounts_email_login_prd091
  ON global_accounts(email_normalized) WHERE login_enabled=1;
CREATE UNIQUE INDEX uq_global_accounts_phone_login_prd091
  ON global_accounts(phone_e164) WHERE login_enabled=1 AND phone_e164 IS NOT NULL;
CREATE INDEX idx_global_accounts_status_prd091
  ON global_accounts(status,login_enabled,updated_at);

CREATE TABLE account_credentials(
  credential_id TEXT PRIMARY KEY CHECK(length(credential_id)=36
    AND substr(credential_id,1,4)='acr_'
    AND substr(credential_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  credential_type TEXT NOT NULL CHECK(credential_type='password'),
  algorithm TEXT NOT NULL CHECK(algorithm IN('pbkdf2_sha256_legacy','argon2id','unknown')),
  secret_hash TEXT NOT NULL CHECK(length(secret_hash) BETWEEN 32 AND 1024),
  status TEXT NOT NULL CHECK(status IN('active','rotated','revoked')),
  version INTEGER NOT NULL CHECK(version>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  rotated_at TEXT CHECK(rotated_at IS NULL OR unixepoch(rotated_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(account_id,credential_id)
);
CREATE UNIQUE INDEX uq_account_credentials_active_password_prd091
  ON account_credentials(account_id) WHERE credential_type='password' AND status='active';

CREATE TABLE account_identity_links(
  identity_link_id TEXT PRIMARY KEY CHECK(length(identity_link_id)=36
    AND substr(identity_link_id,1,4)='ail_'
    AND substr(identity_link_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type='legacy_user'),
  legacy_tenant_id TEXT NOT NULL,
  legacy_user_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status='active'),
  linked_at TEXT NOT NULL CHECK(unixepoch(linked_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(legacy_tenant_id,legacy_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(source_type,legacy_tenant_id,legacy_user_id),
  UNIQUE(account_id,legacy_tenant_id,legacy_user_id)
);
CREATE INDEX idx_account_identity_links_account_prd091
  ON account_identity_links(account_id,identity_link_id);

CREATE TABLE global_sessions(
  session_id TEXT PRIMARY KEY CHECK(length(session_id)=36
    AND substr(session_id,1,4)='gss_'
    AND substr(session_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  token_hmac TEXT NOT NULL UNIQUE CHECK(length(token_hmac)=64
    AND token_hmac NOT GLOB '*[^0-9a-f]*'),
  csrf_hmac TEXT NOT NULL CHECK(length(csrf_hmac)=64
    AND csrf_hmac NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('active','revoked','expired')),
  credential_revision INTEGER NOT NULL CHECK(credential_revision>=1),
  session_revision INTEGER NOT NULL CHECK(session_revision>=1),
  device_family TEXT NOT NULL CHECK(length(device_family) BETWEEN 1 AND 80),
  client_risk_hmac TEXT CHECK(client_risk_hmac IS NULL OR(length(client_risk_hmac)=64
    AND client_risk_hmac NOT GLOB '*[^0-9a-f]*')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  idle_expires_at TEXT NOT NULL CHECK(unixepoch(idle_expires_at) IS NOT NULL),
  last_seen_at TEXT CHECK(last_seen_at IS NULL OR unixepoch(last_seen_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  revoked_reason TEXT CHECK(revoked_reason IS NULL OR revoked_reason IN(
    'logout','user_requested','credential_rotated','account_disabled','revision_changed')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(account_id,session_id),
  CHECK((status='active' AND revoked_at IS NULL AND revoked_reason IS NULL)
    OR(status='revoked' AND revoked_at IS NOT NULL AND revoked_reason IS NOT NULL)
    OR(status='expired'))
);
CREATE INDEX idx_global_sessions_account_prd091
  ON global_sessions(account_id,status,created_at DESC,session_id);
CREATE INDEX idx_global_sessions_expiry_prd091
  ON global_sessions(status,expires_at,idle_expires_at);

CREATE TABLE identity_events(
  identity_event_id TEXT PRIMARY KEY CHECK(length(identity_event_id)=36
    AND substr(identity_event_id,1,4)='iev_'
    AND substr(identity_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT,
  session_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'identity.login_succeeded','identity.login_failed','identity.session_revoked',
    'identity.credential_rotated')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9._]*'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=2048),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(account_id,session_id) REFERENCES global_sessions(account_id,session_id),
  CHECK(event_type='identity.login_failed' OR account_id IS NOT NULL),
  CHECK(event_type NOT IN('identity.login_succeeded','identity.session_revoked')
    OR session_id IS NOT NULL)
);
CREATE INDEX idx_identity_events_account_prd091
  ON identity_events(account_id,occurred_at DESC,identity_event_id);
CREATE INDEX idx_identity_events_request_prd091
  ON identity_events(request_id,event_type);

CREATE TABLE identity_event_outbox(
  outbox_id TEXT PRIMARY KEY CHECK(length(outbox_id)=36
    AND substr(outbox_id,1,4)='iob_'
    AND substr(outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  identity_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','dead')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 20),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
  lease_owner_id TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR(length(last_error_code) BETWEEN 2 AND 80
    AND last_error_code=lower(last_error_code)
    AND last_error_code NOT GLOB '*[^a-z0-9._]*')),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(identity_event_id) REFERENCES identity_events(identity_event_id),
  CHECK((status='pending' AND lease_owner_id IS NULL AND lease_expires_at IS NULL
      AND delivered_at IS NULL)
    OR(status='processing' AND lease_owner_id IS NOT NULL AND lease_expires_at IS NOT NULL
      AND delivered_at IS NULL)
    OR(status='delivered' AND lease_owner_id IS NULL AND lease_expires_at IS NULL
      AND delivered_at IS NOT NULL)
    OR(status='dead' AND lease_owner_id IS NULL AND lease_expires_at IS NULL
      AND delivered_at IS NULL))
);
CREATE INDEX idx_identity_event_outbox_claim_prd091
  ON identity_event_outbox(status,next_attempt_at,created_at,outbox_id);

CREATE TABLE identity_migration_conflicts(
  conflict_id TEXT PRIMARY KEY CHECK(length(conflict_id)=36
    AND substr(conflict_id,1,4)='icf_'
    AND substr(conflict_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL UNIQUE,
  identifier_fingerprint TEXT NOT NULL CHECK(length(identifier_fingerprint)=64
    AND identifier_fingerprint NOT GLOB '*[^0-9a-f]*'),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'duplicate_identifier','identifier_invalid','identifier_requires_claim','credential_unrecognized')),
  status TEXT NOT NULL CHECK(status='open'),
  detected_at TEXT NOT NULL CHECK(unixepoch(detected_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id)
);
CREATE INDEX idx_identity_conflicts_fingerprint_prd091
  ON identity_migration_conflicts(identifier_fingerprint,status,conflict_id);

-- Backfill creates one account per legacy user. Equal email or password facts are not identity proof.
INSERT INTO global_accounts(
  account_id,email_normalized,phone_e164,display_name,status,login_enabled,
  credential_revision,session_revision,mfa_state,failed_login_count,locked_until,
  last_login_at,created_at,updated_at)
SELECT 'gac_'||substr(metahub_sha256(u.tenant_id||char(31)||u.user_id),1,32),
  lower(trim(u.email)),NULL,u.display_name,
  CASE WHEN
      length(trim(u.email))!=length(CAST(trim(u.email) AS BLOB))
      OR length(trim(u.email)) NOT BETWEEN 3 AND 320
      OR instr(trim(u.email),'@')<=1
      OR instr(substr(trim(u.email),instr(trim(u.email),'@')+1),'@')>0
      OR instr(trim(u.email),' ')>0 OR instr(trim(u.email),char(9))>0
      OR instr(trim(u.email),char(10))>0 OR instr(trim(u.email),char(13))>0
      OR coalesce(metahub_password_hash_algorithm(u.password_hash),'unknown')
        !='pbkdf2_sha256_legacy'
      OR (SELECT COUNT(*) FROM users x WHERE lower(trim(x.email))=lower(trim(u.email)))!=1
    THEN 'migration_conflict' ELSE u.status END,
  CASE WHEN
      length(trim(u.email))=length(CAST(trim(u.email) AS BLOB))
      AND length(trim(u.email)) BETWEEN 3 AND 320
      AND instr(trim(u.email),'@')>1
      AND instr(substr(trim(u.email),instr(trim(u.email),'@')+1),'@')=0
      AND instr(trim(u.email),' ')=0 AND instr(trim(u.email),char(9))=0
      AND instr(trim(u.email),char(10))=0 AND instr(trim(u.email),char(13))=0
      AND metahub_password_hash_algorithm(u.password_hash)='pbkdf2_sha256_legacy'
      AND (SELECT COUNT(*) FROM users x WHERE lower(trim(x.email))=lower(trim(u.email)))=1
    THEN 1 ELSE 0 END,
  1,1,'not_enrolled',u.failed_login_count,u.locked_until,u.last_login_at,
  u.created_at,u.updated_at
FROM users u;

INSERT INTO account_credentials(
  credential_id,account_id,credential_type,algorithm,secret_hash,status,version,
  created_at,updated_at,rotated_at)
SELECT 'acr_'||substr(metahub_sha256(u.tenant_id||char(31)||u.user_id),1,32),
  'gac_'||substr(metahub_sha256(u.tenant_id||char(31)||u.user_id),1,32),
  'password',coalesce(metahub_password_hash_algorithm(u.password_hash),'unknown'),
  u.password_hash,'active',1,u.created_at,u.updated_at,NULL
FROM users u;

INSERT INTO account_identity_links(
  identity_link_id,account_id,source_type,legacy_tenant_id,legacy_user_id,status,linked_at)
SELECT 'ail_'||substr(metahub_sha256(u.tenant_id||char(31)||u.user_id),1,32),
  'gac_'||substr(metahub_sha256(u.tenant_id||char(31)||u.user_id),1,32),
  'legacy_user',u.tenant_id,u.user_id,'active',u.created_at
FROM users u;

INSERT INTO identity_migration_conflicts(
  conflict_id,account_id,identifier_fingerprint,reason_code,status,detected_at)
SELECT 'icf_'||substr(metahub_sha256(u.tenant_id||char(31)||u.user_id),1,32),
  a.account_id,metahub_sha256(a.email_normalized),
  CASE WHEN (SELECT COUNT(*) FROM users x
      WHERE lower(trim(x.email))=a.email_normalized)!=1 THEN 'duplicate_identifier'
    WHEN length(trim(u.email))!=length(CAST(trim(u.email) AS BLOB)) THEN 'identifier_requires_claim'
    WHEN coalesce(metahub_password_hash_algorithm(u.password_hash),'unknown')
        !='pbkdf2_sha256_legacy'
      THEN 'credential_unrecognized'
    ELSE 'identifier_invalid' END,
  'open',strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM users u
JOIN account_identity_links l
  ON l.legacy_tenant_id=u.tenant_id AND l.legacy_user_id=u.user_id
JOIN global_accounts a ON a.account_id=l.account_id
WHERE a.status='migration_conflict';

-- Every future legacy user receives an atomic compatibility projection in the same transaction.
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
    NEW.password_hash,'active',1,
    NEW.created_at,NEW.updated_at,NULL);

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

-- Email changes need a verified global claim flow, which is deliberately outside PRD-091.
CREATE TRIGGER trg_users_identity_email_update_prd091
BEFORE UPDATE OF email ON users FOR EACH ROW WHEN NEW.email!=OLD.email
BEGIN SELECT RAISE(ABORT,'identity_email_change_requires_claim'); END;

-- Legacy password writes remain authoritative compatibility input and revoke all global sessions.
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

CREATE TRIGGER trg_account_identity_links_immutable_prd091
BEFORE UPDATE ON account_identity_links FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'identity_link_immutable'); END;
CREATE TRIGGER trg_account_identity_links_delete_prd091
BEFORE DELETE ON account_identity_links FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'identity_link_immutable'); END;
CREATE TRIGGER trg_identity_events_update_prd091
BEFORE UPDATE ON identity_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'identity_event_immutable'); END;
CREATE TRIGGER trg_identity_events_delete_prd091
BEFORE DELETE ON identity_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'identity_event_immutable'); END;
CREATE TRIGGER trg_identity_conflicts_update_prd091
BEFORE UPDATE ON identity_migration_conflicts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'identity_conflict_immutable'); END;
CREATE TRIGGER trg_identity_conflicts_delete_prd091
BEFORE DELETE ON identity_migration_conflicts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'identity_conflict_immutable'); END;

CREATE TEMP TABLE prd091_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd091_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM global_accounts)=(SELECT COUNT(*) FROM users)
    AND (SELECT COUNT(*) FROM account_credentials)=(SELECT COUNT(*) FROM users)
    AND (SELECT COUNT(*) FROM account_identity_links)=(SELECT COUNT(*) FROM users)
    AND NOT EXISTS(
      SELECT 1 FROM users u JOIN account_identity_links l
        ON l.legacy_tenant_id=u.tenant_id AND l.legacy_user_id=u.user_id
      JOIN account_credentials c ON c.account_id=l.account_id
      WHERE c.secret_hash!=u.password_hash)
    AND (SELECT COUNT(*) FROM pragma_foreign_key_check)=0
    AND (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check='ok')=1
  THEN 0 ELSE 1 END;
DROP TABLE prd091_up_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
