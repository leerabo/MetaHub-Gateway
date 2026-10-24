-- Retain sessions and all foreign-key targets while making MFA opt-in.
PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;
BEGIN IMMEDIATE;
CREATE TABLE platform_sessions_optional_mfa(
  platform_session_id TEXT PRIMARY KEY CHECK(length(platform_session_id)=36
    AND substr(platform_session_id,1,4)='pts_'),
  operator_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  global_session_id TEXT NOT NULL,
  factor_id TEXT,
  token_hmac TEXT NOT NULL UNIQUE CHECK(length(token_hmac)=64),
  csrf_hmac TEXT NOT NULL CHECK(length(csrf_hmac)=64),
  status TEXT NOT NULL CHECK(status IN('active','revoked','expired')),
  operator_revision INTEGER NOT NULL,
  factor_revision INTEGER NOT NULL CHECK((factor_id IS NULL AND factor_revision=0) OR (factor_id IS NOT NULL AND factor_revision>=1)),
  role_fingerprint TEXT NOT NULL CHECK(length(role_fingerprint)=64),
  last_seen_at TEXT NOT NULL CHECK(unixepoch(last_seen_at) IS NOT NULL),
  absolute_expires_at TEXT NOT NULL CHECK(unixepoch(absolute_expires_at) IS NOT NULL),
  idle_expires_at TEXT NOT NULL CHECK(unixepoch(idle_expires_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  revoked_reason TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(factor_id) REFERENCES account_mfa_factors(factor_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(global_session_id) REFERENCES global_sessions(session_id),
  FOREIGN KEY(account_id,factor_id) REFERENCES account_mfa_factors(account_id,factor_id)
);
INSERT INTO platform_sessions_optional_mfa SELECT * FROM platform_sessions;
DROP TABLE platform_sessions;
ALTER TABLE platform_sessions_optional_mfa RENAME TO platform_sessions;
CREATE INDEX idx_platform_sessions_operator_prd095
  ON platform_sessions(operator_id,status,absolute_expires_at);

CREATE TEMP TABLE optional_mfa_integrity_guard(issue_count INTEGER CHECK(issue_count=0));
INSERT INTO optional_mfa_integrity_guard SELECT COUNT(*) FROM pragma_foreign_key_check;
DROP TABLE optional_mfa_integrity_guard;
COMMIT;
PRAGMA legacy_alter_table=OFF;
PRAGMA foreign_keys=ON;
