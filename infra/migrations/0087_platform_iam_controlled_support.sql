PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd095_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd095_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0086' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'global_accounts','global_sessions','workspace_tenant_bindings','workspace_memberships',
      'workspace_sessions'))=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd095')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd095_up_guard;

CREATE TABLE account_mfa_factors(
  factor_id TEXT PRIMARY KEY CHECK(length(factor_id)=36 AND substr(factor_id,1,4)='mfa_'
    AND substr(factor_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  factor_type TEXT NOT NULL CHECK(factor_type='totp'),
  secret_ciphertext TEXT NOT NULL CHECK(length(secret_ciphertext) BETWEEN 80 AND 1024),
  secret_version INTEGER NOT NULL CHECK(secret_version=1),
  status TEXT NOT NULL CHECK(status IN('pending','active','disabled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  last_accepted_counter INTEGER CHECK(last_accepted_counter IS NULL OR last_accepted_counter>=0),
  verified_at TEXT CHECK(verified_at IS NULL OR unixepoch(verified_at) IS NOT NULL),
  disabled_at TEXT CHECK(disabled_at IS NULL OR unixepoch(disabled_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(account_id,factor_id),
  CHECK((status='pending' AND verified_at IS NULL AND disabled_at IS NULL
      AND last_accepted_counter IS NULL)
    OR(status='active' AND verified_at IS NOT NULL AND disabled_at IS NULL
      AND last_accepted_counter IS NOT NULL)
    OR(status='disabled' AND disabled_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_account_mfa_active_prd095 ON account_mfa_factors(account_id)
  WHERE status='active';
CREATE UNIQUE INDEX uq_account_mfa_pending_prd095 ON account_mfa_factors(account_id)
  WHERE status='pending';
CREATE TABLE account_mfa_attempt_guards(
  account_id TEXT NOT NULL,
  factor_id TEXT NOT NULL,
  device_hmac TEXT NOT NULL CHECK(length(device_hmac)=64
    AND device_hmac NOT GLOB '*[^0-9a-f]*'),
  failed_count INTEGER NOT NULL CHECK(failed_count BETWEEN 0 AND 20),
  locked_until TEXT CHECK(locked_until IS NULL OR unixepoch(locked_until) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(account_id,factor_id,device_hmac),
  FOREIGN KEY(account_id,factor_id) REFERENCES account_mfa_factors(account_id,factor_id)
);

CREATE TABLE platform_roles(
  role_code TEXT PRIMARY KEY CHECK(role_code IN(
    'platform_owner','platform_ops','platform_security','platform_billing',
    'platform_support','platform_verification','platform_compliance','platform_privacy',
    'release_manager','platform_provider_engineer','platform_channel',
    'platform_customer_success','platform_growth')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1)
);
CREATE TABLE platform_permissions(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1)
);
CREATE TABLE platform_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_permissions(permission_code)
);
INSERT INTO platform_roles(role_code,catalog_revision) VALUES
 ('platform_owner',1),('platform_ops',1),('platform_security',1),
 ('platform_billing',1),('platform_support',1),('platform_verification',1),
 ('platform_compliance',1),('platform_privacy',1),('release_manager',1),
 ('platform_provider_engineer',1),('platform_channel',1),
 ('platform_customer_success',1),('platform_growth',1);
INSERT INTO platform_permissions(permission_code,catalog_revision) VALUES
 ('platform.navigation.read',1),('platform.operator.manage',1),
 ('platform.audit.read',1),('platform.support.request',1),
 ('platform.support.approve',1),('platform.break_glass.use',1),
 ('platform.billing.read',1),('platform.verification.read',1),
 ('platform.compliance.read',1),('platform.privacy.read',1),
 ('platform.release.read',1),('platform.provider.read',1),
 ('platform.channel.read',1),('platform.customer_success.read',1),
 ('platform.growth.read',1),('platform.workspace.directory.read',1),
 ('platform.workspace.lifecycle.manage',1),('platform.workspace.risk.manage',1);
INSERT INTO platform_role_permissions(role_code,permission_code)
SELECT role.role_code,'platform.navigation.read' FROM platform_roles role;
INSERT INTO platform_role_permissions(role_code,permission_code) VALUES
 ('platform_owner','platform.operator.manage'),('platform_owner','platform.audit.read'),
 ('platform_owner','platform.support.request'),('platform_owner','platform.support.approve'),
 ('platform_owner','platform.break_glass.use'),('platform_owner','platform.workspace.directory.read'),
 ('platform_owner','platform.workspace.lifecycle.manage'),('platform_owner','platform.workspace.risk.manage'),
 ('platform_ops','platform.workspace.directory.read'),('platform_ops','platform.workspace.lifecycle.manage'),
 ('platform_security','platform.audit.read'),('platform_security','platform.support.approve'),
 ('platform_security','platform.break_glass.use'),('platform_security','platform.workspace.risk.manage'),
 ('platform_billing','platform.billing.read'),
 ('platform_support','platform.support.request'),('platform_support','platform.workspace.directory.read'),
 ('platform_support','platform.audit.read'),
 ('platform_verification','platform.verification.read'),
 ('platform_compliance','platform.compliance.read'),('platform_compliance','platform.audit.read'),
 ('platform_compliance','platform.support.approve'),
 ('platform_privacy','platform.privacy.read'),('release_manager','platform.release.read'),
 ('platform_provider_engineer','platform.provider.read'),('platform_channel','platform.channel.read'),
 ('platform_customer_success','platform.customer_success.read'),
 ('platform_customer_success','platform.workspace.directory.read'),
 ('platform_growth','platform.growth.read');
INSERT OR IGNORE INTO platform_role_permissions(role_code,permission_code)
SELECT 'platform_owner',permission_code FROM platform_permissions;
CREATE TABLE platform_catalog_state(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1),
  catalog_sha256 TEXT NOT NULL CHECK(length(catalog_sha256)=64
    AND catalog_sha256 NOT GLOB '*[^0-9a-f]*')
);
INSERT INTO platform_catalog_state(singleton_id,catalog_revision,catalog_sha256)
VALUES(1,1,'cded0f381ab07e93ca91b40bd7d95bcc1bf10ca47b71a51e2fd0f3b6043e7afc');
CREATE TRIGGER trg_platform_roles_insert_prd095 BEFORE INSERT ON platform_roles
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_roles_update_prd095 BEFORE UPDATE ON platform_roles
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_roles_delete_prd095 BEFORE DELETE ON platform_roles
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_permissions_insert_prd095 BEFORE INSERT ON platform_permissions
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_permissions_update_prd095 BEFORE UPDATE ON platform_permissions
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_permissions_delete_prd095 BEFORE DELETE ON platform_permissions
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_role_permissions_insert_prd095
BEFORE INSERT ON platform_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_role_permissions_update_prd095
BEFORE UPDATE ON platform_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_role_permissions_delete_prd095
BEFORE DELETE ON platform_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_catalog_state_update_prd095 BEFORE UPDATE ON platform_catalog_state
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_catalog_state_delete_prd095 BEFORE DELETE ON platform_catalog_state
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;

CREATE TABLE platform_operators(
  operator_id TEXT PRIMARY KEY CHECK(length(operator_id)=36 AND substr(operator_id,1,4)='pto_'
    AND substr(operator_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id)
);
CREATE TABLE platform_operator_role_bindings(
  operator_id TEXT NOT NULL,
  role_code TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('active','revoked')),
  granted_by_operator_id TEXT,
  granted_at TEXT NOT NULL CHECK(unixepoch(granted_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  PRIMARY KEY(operator_id,role_code),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(granted_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status='active' AND revoked_at IS NULL) OR(status='revoked' AND revoked_at IS NOT NULL))
);
CREATE TABLE platform_bootstrap_state(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  status TEXT NOT NULL CHECK(status IN('open','completed')),
  operator_id TEXT UNIQUE,
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  CHECK((status='open' AND operator_id IS NULL AND completed_at IS NULL)
    OR(status='completed' AND operator_id IS NOT NULL AND completed_at IS NOT NULL)),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id)
);
INSERT INTO platform_bootstrap_state(singleton_id,status,operator_id,completed_at)
VALUES(1,'open',NULL,NULL);

CREATE TABLE platform_sessions(
  platform_session_id TEXT PRIMARY KEY CHECK(length(platform_session_id)=36
    AND substr(platform_session_id,1,4)='pts_'),
  operator_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  global_session_id TEXT NOT NULL,
  factor_id TEXT NOT NULL,
  token_hmac TEXT NOT NULL UNIQUE CHECK(length(token_hmac)=64),
  csrf_hmac TEXT NOT NULL CHECK(length(csrf_hmac)=64),
  status TEXT NOT NULL CHECK(status IN('active','revoked','expired')),
  operator_revision INTEGER NOT NULL,
  factor_revision INTEGER NOT NULL,
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
CREATE INDEX idx_platform_sessions_operator_prd095
  ON platform_sessions(operator_id,status,absolute_expires_at);

CREATE TABLE support_grants(
  grant_id TEXT PRIMARY KEY CHECK(length(grant_id)=36 AND substr(grant_id,1,4)='sgr_'),
  requester_operator_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  scope_code TEXT NOT NULL CHECK(scope_code='workspace.diagnostics.read'),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80),
  status TEXT NOT NULL CHECK(status IN('pending_approvals','active','revoked','expired')),
  operator_revision INTEGER NOT NULL,
  workspace_revision INTEGER NOT NULL,
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(requester_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_support_grants_requester_prd095
  ON support_grants(requester_operator_id,status,created_at DESC,grant_id);
CREATE TABLE support_grant_approvals(
  approval_id TEXT PRIMARY KEY CHECK(length(approval_id)=36 AND substr(approval_id,1,4)='sap_'),
  grant_id TEXT NOT NULL,
  approval_domain TEXT NOT NULL CHECK(approval_domain IN('workspace','platform')),
  operator_id TEXT,
  membership_id TEXT,
  authority_revision INTEGER NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(grant_id) REFERENCES support_grants(grant_id),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  UNIQUE(grant_id,approval_domain),
  CHECK((approval_domain='platform' AND operator_id IS NOT NULL AND membership_id IS NULL)
    OR(approval_domain='workspace' AND membership_id IS NOT NULL AND operator_id IS NULL))
);
CREATE TABLE support_sessions(
  support_session_id TEXT PRIMARY KEY CHECK(length(support_session_id)=36
    AND substr(support_session_id,1,4)='sps_'),
  source_kind TEXT NOT NULL CHECK(source_kind IN('grant','break_glass')),
  source_id TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  factor_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  scope_code TEXT NOT NULL CHECK(scope_code='workspace.diagnostics.read'),
  token_hmac TEXT NOT NULL UNIQUE CHECK(length(token_hmac)=64),
  status TEXT NOT NULL CHECK(status IN('active','revoked','expired')),
  operator_revision INTEGER NOT NULL,
  factor_revision INTEGER NOT NULL,
  absolute_expires_at TEXT NOT NULL CHECK(unixepoch(absolute_expires_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(factor_id) REFERENCES account_mfa_factors(factor_id),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);

CREATE TABLE break_glass_sessions(
  break_glass_id TEXT PRIMARY KEY CHECK(length(break_glass_id)=36
    AND substr(break_glass_id,1,4)='bgs_'),
  requester_operator_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80),
  status TEXT NOT NULL CHECK(status IN('active','review_pending','reviewed','expired')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  review_due_at TEXT NOT NULL CHECK(unixepoch(review_due_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(requester_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE TABLE break_glass_reviews(
  review_id TEXT PRIMARY KEY CHECK(length(review_id)=36 AND substr(review_id,1,4)='bgr_'),
  break_glass_id TEXT NOT NULL,
  review_domain TEXT NOT NULL CHECK(review_domain IN('workspace','platform')),
  operator_id TEXT,
  membership_id TEXT,
  authority_revision INTEGER NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(break_glass_id) REFERENCES break_glass_sessions(break_glass_id),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  UNIQUE(break_glass_id,review_domain),
  CHECK((review_domain='platform' AND operator_id IS NOT NULL AND membership_id IS NULL)
    OR(review_domain='workspace' AND membership_id IS NOT NULL AND operator_id IS NULL))
);

CREATE TABLE platform_audit_logs(
  audit_id TEXT PRIMARY KEY CHECK(length(audit_id)=36 AND substr(audit_id,1,4)='pau_'),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('global','platform','workspace','support')),
  actor_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(length(action_code) BETWEEN 2 AND 80),
  object_type TEXT NOT NULL CHECK(length(object_type) BETWEEN 2 AND 40),
  object_id TEXT NOT NULL,
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  before_sha256 TEXT CHECK(before_sha256 IS NULL OR length(before_sha256)=64),
  after_sha256 TEXT CHECK(after_sha256 IS NULL OR length(after_sha256)=64),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL)
);
CREATE INDEX idx_platform_audit_time_prd095
  ON platform_audit_logs(occurred_at DESC,audit_id DESC);
CREATE TABLE platform_event_outbox(
  outbox_id TEXT PRIMARY KEY CHECK(length(outbox_id)=36 AND substr(outbox_id,1,4)='peo_'),
  audit_id TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL CHECK(length(event_type) BETWEEN 2 AND 80),
  priority TEXT NOT NULL CHECK(priority IN('normal','high')),
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','dead')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 20),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(audit_id) REFERENCES platform_audit_logs(audit_id)
);

CREATE TRIGGER trg_platform_audit_update_prd095 BEFORE UPDATE ON platform_audit_logs
BEGIN SELECT RAISE(ABORT,'platform_audit_immutable'); END;
CREATE TRIGGER trg_platform_audit_delete_prd095 BEFORE DELETE ON platform_audit_logs
BEGIN SELECT RAISE(ABORT,'platform_audit_immutable'); END;
CREATE TRIGGER trg_platform_operator_revoke_prd095
AFTER UPDATE OF status,revision ON platform_operators FOR EACH ROW
BEGIN
  UPDATE platform_sessions SET status='revoked',revoked_at=NEW.updated_at,
    revoked_reason='operator_changed',updated_at=NEW.updated_at
    WHERE operator_id=NEW.operator_id AND status='active';
  UPDATE support_sessions SET status='revoked',revoked_at=NEW.updated_at,updated_at=NEW.updated_at
    WHERE operator_id=NEW.operator_id AND status='active';
  UPDATE support_grants SET status='revoked',revoked_at=NEW.updated_at,updated_at=NEW.updated_at
    WHERE requester_operator_id=NEW.operator_id AND status IN('pending_approvals','active');
END;
CREATE TRIGGER trg_global_session_revoke_platform_prd095
AFTER UPDATE OF status ON global_sessions FOR EACH ROW WHEN NEW.status!='active'
BEGIN
  UPDATE platform_sessions SET status='revoked',revoked_at=NEW.updated_at,
    revoked_reason='global_session_changed',updated_at=NEW.updated_at
    WHERE global_session_id=NEW.session_id AND status='active';
END;

CREATE TEMP TABLE prd095_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd095_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_roles)=13 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_role_permissions)=51 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT catalog_revision||':'||catalog_sha256 FROM platform_catalog_state
      WHERE singleton_id=1)='1:cded0f381ab07e93ca91b40bd7d95bcc1bf10ca47b71a51e2fd0f3b6043e7afc'
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_bootstrap_state)=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd095_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
