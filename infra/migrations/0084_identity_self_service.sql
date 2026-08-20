PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd092_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd092_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0083' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd092')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd092_up_guard;

CREATE TABLE registration_intents(
  registration_intent_id TEXT PRIMARY KEY CHECK(length(registration_intent_id)=36
    AND substr(registration_intent_id,1,4)='rgi_'
    AND substr(registration_intent_id,5) NOT GLOB '*[^0-9a-f]*'),
  email_ciphertext TEXT NOT NULL CHECK(length(email_ciphertext) BETWEEN 80 AND 768
    AND email_ciphertext GLOB 'metahub-identity-pii-aesgcm-v1:*'),
  email_hmac TEXT NOT NULL CHECK(length(email_hmac)=64
    AND email_hmac NOT GLOB '*[^0-9a-f]*'),
  display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 120),
  candidate_password_hash TEXT CHECK(candidate_password_hash IS NULL
    OR length(candidate_password_hash) BETWEEN 32 AND 1024),
  intent_kind TEXT NOT NULL CHECK(intent_kind IN('new_account','existing_account','sink')),
  target_account_id TEXT,
  target_credential_revision INTEGER CHECK(
    target_credential_revision IS NULL OR target_credential_revision>=1),
  invitation_id TEXT,
  consent_version TEXT NOT NULL CHECK(length(consent_version) BETWEEN 1 AND 80),
  status TEXT NOT NULL CHECK(status IN(
    'pending','verified','expired','superseded','sink_consumed')),
  generation INTEGER NOT NULL DEFAULT 1 CHECK(generation>=1),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  verified_at TEXT CHECK(verified_at IS NULL OR unixepoch(verified_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(target_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(invitation_id) REFERENCES invitations(invitation_id),
  CHECK((intent_kind='existing_account' AND target_account_id IS NOT NULL
      AND target_credential_revision IS NOT NULL AND candidate_password_hash IS NULL)
    OR(intent_kind IN('new_account','sink') AND target_account_id IS NULL
      AND target_credential_revision IS NULL AND candidate_password_hash IS NOT NULL)),
  CHECK((status IN('pending','superseded','expired') AND verified_at IS NULL)
    OR(status IN('verified','sink_consumed') AND verified_at IS NOT NULL)),
  CHECK(unixepoch(expires_at)>unixepoch(created_at))
);
CREATE INDEX idx_registration_intents_email_prd092
  ON registration_intents(email_hmac,status,created_at DESC);
CREATE INDEX idx_registration_intents_expiry_prd092
  ON registration_intents(status,expires_at,registration_intent_id);

CREATE TABLE password_reset_intents(
  password_reset_intent_id TEXT PRIMARY KEY CHECK(length(password_reset_intent_id)=36
    AND substr(password_reset_intent_id,1,4)='pri_'
    AND substr(password_reset_intent_id,5) NOT GLOB '*[^0-9a-f]*'),
  email_hmac TEXT NOT NULL CHECK(length(email_hmac)=64
    AND email_hmac NOT GLOB '*[^0-9a-f]*'),
  intent_kind TEXT NOT NULL CHECK(intent_kind IN('account','sink')),
  target_account_id TEXT,
  target_credential_revision INTEGER CHECK(
    target_credential_revision IS NULL OR target_credential_revision>=1),
  status TEXT NOT NULL CHECK(status IN('pending','completed','expired','exhausted','sink_consumed')),
  generation INTEGER NOT NULL DEFAULT 1 CHECK(generation>=1),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(target_account_id) REFERENCES global_accounts(account_id),
  CHECK((intent_kind='account' AND target_account_id IS NOT NULL
      AND target_credential_revision IS NOT NULL)
    OR(intent_kind='sink' AND target_account_id IS NULL
      AND target_credential_revision IS NULL)),
  CHECK((status IN('pending','expired','exhausted') AND completed_at IS NULL)
    OR(status IN('completed','sink_consumed') AND completed_at IS NOT NULL)),
  CHECK(unixepoch(expires_at)>unixepoch(created_at))
);
CREATE INDEX idx_password_reset_intents_email_prd092
  ON password_reset_intents(email_hmac,status,created_at DESC);
CREATE INDEX idx_password_reset_intents_expiry_prd092
  ON password_reset_intents(status,expires_at,password_reset_intent_id);

CREATE TABLE verification_challenges(
  verification_challenge_id TEXT PRIMARY KEY CHECK(length(verification_challenge_id)=36
    AND substr(verification_challenge_id,1,4)='vch_'
    AND substr(verification_challenge_id,5) NOT GLOB '*[^0-9a-f]*'),
  registration_intent_id TEXT,
  password_reset_intent_id TEXT,
  purpose TEXT NOT NULL CHECK(purpose IN('registration','password_reset')),
  code_hmac TEXT NOT NULL UNIQUE CHECK(length(code_hmac)=64
    AND code_hmac NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('pending','consumed','expired','exhausted','superseded')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 5),
  sent_at TEXT NOT NULL CHECK(unixepoch(sent_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(registration_intent_id) REFERENCES registration_intents(registration_intent_id),
  FOREIGN KEY(password_reset_intent_id) REFERENCES password_reset_intents(password_reset_intent_id),
  CHECK((purpose='registration' AND registration_intent_id IS NOT NULL
      AND password_reset_intent_id IS NULL)
    OR(purpose='password_reset' AND registration_intent_id IS NULL
      AND password_reset_intent_id IS NOT NULL)),
  CHECK((status='consumed' AND consumed_at IS NOT NULL)
    OR(status!='consumed' AND consumed_at IS NULL)),
  CHECK(unixepoch(expires_at)>unixepoch(sent_at))
);
CREATE INDEX idx_verification_challenges_registration_prd092
  ON verification_challenges(registration_intent_id,generation,status);
CREATE INDEX idx_verification_challenges_reset_prd092
  ON verification_challenges(password_reset_intent_id,generation,status);

CREATE TABLE account_verifications(
  account_verification_id TEXT PRIMARY KEY CHECK(length(account_verification_id)=36
    AND substr(account_verification_id,1,4)='avf_'
    AND substr(account_verification_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  verification_type TEXT NOT NULL CHECK(verification_type='email'),
  identifier_hmac TEXT NOT NULL CHECK(length(identifier_hmac)=64
    AND identifier_hmac NOT GLOB '*[^0-9a-f]*'),
  source_type TEXT NOT NULL CHECK(source_type IN('registration','password_reset')),
  source_intent_id TEXT NOT NULL CHECK(length(source_intent_id)=36),
  verified_at TEXT NOT NULL CHECK(unixepoch(verified_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(account_id,verification_type,identifier_hmac)
);
CREATE TRIGGER trg_account_verifications_update_prd092
BEFORE UPDATE ON account_verifications FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'account_verification_immutable'); END;
CREATE TRIGGER trg_account_verifications_delete_prd092
BEFORE DELETE ON account_verifications FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'account_verification_immutable'); END;

CREATE TABLE consent_records(
  consent_record_id TEXT PRIMARY KEY CHECK(length(consent_record_id)=36
    AND substr(consent_record_id,1,4)='con_'
    AND substr(consent_record_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  consent_type TEXT NOT NULL CHECK(consent_type='privacy_terms'),
  consent_version TEXT NOT NULL CHECK(length(consent_version) BETWEEN 1 AND 80),
  source_registration_intent_id TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  consented_at TEXT NOT NULL CHECK(unixepoch(consented_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(source_registration_intent_id)
    REFERENCES registration_intents(registration_intent_id),
  UNIQUE(account_id,consent_type,consent_version,source_registration_intent_id)
);
CREATE TRIGGER trg_consent_records_update_prd092
BEFORE UPDATE ON consent_records FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'consent_record_immutable'); END;
CREATE TRIGGER trg_consent_records_delete_prd092
BEFORE DELETE ON consent_records FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'consent_record_immutable'); END;

CREATE TABLE identity_abuse_buckets(
  identity_abuse_bucket_id TEXT PRIMARY KEY CHECK(length(identity_abuse_bucket_id)=36
    AND substr(identity_abuse_bucket_id,1,4)='iab_'
    AND substr(identity_abuse_bucket_id,5) NOT GLOB '*[^0-9a-f]*'),
  scope TEXT NOT NULL CHECK(scope IN(
    'registration_email','registration_client','registration_device','registration_global',
    'verification_intent','verification_client','verification_device','verification_global',
    'reset_email','reset_client','reset_device','reset_global')),
  bucket_hmac TEXT NOT NULL CHECK(length(bucket_hmac)=64
    AND bucket_hmac NOT GLOB '*[^0-9a-f]*'),
  window_started_at TEXT NOT NULL CHECK(unixepoch(window_started_at) IS NOT NULL),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 1 AND 10000),
  blocked_until TEXT CHECK(blocked_until IS NULL OR unixepoch(blocked_until) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(scope,bucket_hmac,window_started_at),
  CHECK(unixepoch(expires_at)>unixepoch(window_started_at))
);
CREATE INDEX idx_identity_abuse_buckets_lookup_prd092
  ON identity_abuse_buckets(scope,bucket_hmac,expires_at);

CREATE TABLE invitation_identity_bindings(
  invitation_identity_binding_id TEXT PRIMARY KEY CHECK(
    length(invitation_identity_binding_id)=36
    AND substr(invitation_identity_binding_id,1,4)='iib_'
    AND substr(invitation_identity_binding_id,5) NOT GLOB '*[^0-9a-f]*'),
  invitation_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  registration_intent_id TEXT NOT NULL UNIQUE,
  email_fingerprint TEXT NOT NULL CHECK(length(email_fingerprint)=64
    AND email_fingerprint NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status='verified_pending_membership'),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision=1),
  verified_at TEXT NOT NULL CHECK(unixepoch(verified_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,invitation_id) REFERENCES invitations(tenant_id,invitation_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(registration_intent_id)
    REFERENCES registration_intents(registration_intent_id)
);
CREATE TRIGGER trg_invitation_identity_bindings_update_prd092
BEFORE UPDATE ON invitation_identity_bindings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'invitation_identity_binding_immutable'); END;
CREATE TRIGGER trg_invitation_identity_bindings_delete_prd092
BEFORE DELETE ON invitation_identity_bindings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'invitation_identity_binding_immutable'); END;

CREATE TABLE identity_lifecycle_events(
  identity_lifecycle_event_id TEXT PRIMARY KEY CHECK(length(identity_lifecycle_event_id)=36
    AND substr(identity_lifecycle_event_id,1,4)='ile_'
    AND substr(identity_lifecycle_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT,
  source_intent_id TEXT NOT NULL CHECK(length(source_intent_id)=36),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'identity.challenge_dispatched','identity.challenge_delivery_failed',
    'identity.registered','identity.verified','identity.password_reset')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9._]*'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=2048),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  CHECK((event_type IN('identity.challenge_dispatched','identity.challenge_delivery_failed')
      AND account_id IS NULL)
    OR(event_type IN('identity.registered','identity.verified','identity.password_reset')
      AND account_id IS NOT NULL))
);
CREATE INDEX idx_identity_lifecycle_events_account_prd092
  ON identity_lifecycle_events(account_id,occurred_at DESC,identity_lifecycle_event_id);
CREATE TRIGGER trg_identity_lifecycle_events_update_prd092
BEFORE UPDATE ON identity_lifecycle_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'identity_lifecycle_event_immutable'); END;
CREATE TRIGGER trg_identity_lifecycle_events_delete_prd092
BEFORE DELETE ON identity_lifecycle_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'identity_lifecycle_event_immutable'); END;

CREATE TABLE identity_lifecycle_outbox(
  identity_lifecycle_outbox_id TEXT PRIMARY KEY CHECK(
    length(identity_lifecycle_outbox_id)=36
    AND substr(identity_lifecycle_outbox_id,1,4)='ilo_'
    AND substr(identity_lifecycle_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  identity_lifecycle_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','dead')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 20),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(identity_lifecycle_event_id)
    REFERENCES identity_lifecycle_events(identity_lifecycle_event_id),
  CHECK((status IN('pending','processing','dead') AND delivered_at IS NULL)
    OR(status='delivered' AND delivered_at IS NOT NULL))
);
CREATE INDEX idx_identity_lifecycle_outbox_claim_prd092
  ON identity_lifecycle_outbox(status,next_attempt_at,created_at,
    identity_lifecycle_outbox_id);

CREATE TEMP TABLE prd092_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd092_up_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd092_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
