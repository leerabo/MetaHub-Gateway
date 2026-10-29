PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd094_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd094_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0085' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspaces','workspace_tenant_bindings','workspace_memberships','global_sessions',
      'account_identity_links','invitation_identity_bindings'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd094')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd094_up_guard;

CREATE TABLE workspace_membership_migration_conflicts(
  workspace_id TEXT PRIMARY KEY,
  previous_workspace_status TEXT NOT NULL CHECK(previous_workspace_status IN('active','migration_conflict')),
  previous_profile_state TEXT NOT NULL CHECK(previous_profile_state IN('draft','confirmed','migration_conflict')),
  previous_revision INTEGER NOT NULL CHECK(previous_revision>=1),
  reason_code TEXT NOT NULL CHECK(reason_code='duplicate_account_links'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id)
);
CREATE TRIGGER trg_workspace_membership_conflicts_update_prd094
BEFORE UPDATE ON workspace_membership_migration_conflicts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_membership_conflict_immutable'); END;
CREATE TRIGGER trg_workspace_membership_conflicts_delete_prd094
BEFORE DELETE ON workspace_membership_migration_conflicts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_membership_conflict_immutable'); END;

INSERT INTO workspace_membership_migration_conflicts(
  workspace_id,previous_workspace_status,previous_profile_state,previous_revision,
  reason_code,created_at)
SELECT DISTINCT binding.workspace_id,workspace.status,workspace.profile_state,workspace.revision,
  'duplicate_account_links',strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM workspace_tenant_bindings binding
JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
JOIN account_identity_links link ON link.legacy_tenant_id=binding.tenant_id
GROUP BY binding.workspace_id,link.account_id
HAVING COUNT(*)>1;

UPDATE workspaces
SET status='migration_conflict',profile_state='migration_conflict',
    revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
WHERE workspace_id IN(SELECT workspace_id FROM workspace_membership_migration_conflicts)
  AND status!='migration_conflict';

DROP TRIGGER trg_workspace_memberships_update_prd093;
DROP TRIGGER trg_workspace_memberships_delete_prd093;
ALTER TABLE workspace_memberships RENAME TO workspace_memberships_prd093;

CREATE TABLE workspace_memberships(
  workspace_membership_id TEXT PRIMARY KEY CHECK(length(workspace_membership_id)=36
    AND substr(workspace_membership_id,1,4)='wmb_'
    AND substr(workspace_membership_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  legacy_user_id TEXT,
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin',
    'auditor','viewer','service_account')),
  status TEXT NOT NULL CHECK(status IN('pending','active','disabled')),
  source TEXT NOT NULL CHECK(source IN(
    'workspace_creator','legacy_backfill','membership_invitation')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  created_by_account_id TEXT,
  updated_by_account_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(tenant_id,legacy_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY(created_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(updated_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,account_id),
  UNIQUE(tenant_id,legacy_user_id),
  CHECK((role_code='service_account' AND legacy_user_id IS NULL AND status!='active')
    OR(role_code!='service_account' AND legacy_user_id IS NOT NULL))
);
CREATE INDEX idx_workspace_memberships_account_prd094
  ON workspace_memberships(account_id,status,created_at,workspace_id);
CREATE INDEX idx_workspace_memberships_workspace_prd094
  ON workspace_memberships(workspace_id,status,role_code,created_at,workspace_membership_id);

INSERT INTO workspace_memberships(
  workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
  role_code,status,source,revision,created_by_account_id,
  updated_by_account_id,created_at,updated_at)
SELECT workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
  'owner','active',source,1,account_id,account_id,created_at,updated_at
FROM workspace_memberships_prd093;

INSERT INTO workspace_memberships(
  workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
  role_code,status,source,revision,created_by_account_id,
  updated_by_account_id,created_at,updated_at)
SELECT 'wmb_'||lower(hex(randomblob(16))),binding.workspace_id,binding.tenant_id,
  link.account_id,link.legacy_user_id,
  CASE user.role
    WHEN 'admin' THEN 'admin'
    WHEN 'ops_admin' THEN 'admin'
    WHEN 'developer' THEN 'developer'
    WHEN 'security_admin' THEN 'security_admin'
    WHEN 'finance_admin' THEN 'finance_admin'
    ELSE 'viewer'
  END,
  CASE WHEN user.status='active' THEN 'active' ELSE 'disabled' END,
  'legacy_backfill',1,link.account_id,link.account_id,user.created_at,user.updated_at
FROM workspace_tenant_bindings binding
JOIN account_identity_links link ON link.legacy_tenant_id=binding.tenant_id
JOIN users user ON user.tenant_id=link.legacy_tenant_id AND user.user_id=link.legacy_user_id
WHERE NOT EXISTS(SELECT 1 FROM workspace_memberships current
    WHERE current.workspace_id=binding.workspace_id AND current.account_id=link.account_id)
  AND NOT EXISTS(SELECT 1 FROM workspace_membership_migration_conflicts conflict
    WHERE conflict.workspace_id=binding.workspace_id);
DROP TABLE workspace_memberships_prd093;

-- PRD-093 finalized a legacy Workspace whenever the newly inserted User had any
-- Membership.  After PRD-094 backfills every legacy User, that would try to replace
-- the immutable creator with each non-owner projection and abort the User insert.
-- Preserve the established owner for that exact compatibility side effect while
-- retaining the original abort contract for every other immutable-field update.
DROP TRIGGER trg_workspaces_owner_update_prd093;
CREATE TRIGGER trg_workspaces_owner_update_prd093
BEFORE UPDATE ON workspaces FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.kind!=OLD.kind
  OR NEW.creation_source!=OLD.creation_source
  OR (NEW.created_by_account_id IS NOT OLD.created_by_account_id AND NOT (
    OLD.creation_source IN('legacy_bootstrap','legacy_backfill')
    AND OLD.status='migration_conflict' AND NEW.status='active'
    AND OLD.created_by_account_id IS NULL AND NEW.created_by_account_id IS NOT NULL
  ))
  OR NEW.created_at!=OLD.created_at
BEGIN
  SELECT CASE WHEN
    NEW.workspace_id=OLD.workspace_id AND NEW.kind=OLD.kind
    AND NEW.creation_source=OLD.creation_source AND NEW.created_at=OLD.created_at
    AND OLD.creation_source IN('legacy_bootstrap','legacy_backfill')
    AND OLD.status='active' AND NEW.status='active'
    AND OLD.profile_state='confirmed' AND NEW.profile_state='confirmed'
    AND OLD.created_by_account_id IS NOT NULL
    AND NEW.created_by_account_id IS NOT OLD.created_by_account_id
    AND EXISTS(SELECT 1 FROM workspace_memberships owner_member
      WHERE owner_member.workspace_id=OLD.workspace_id
        AND owner_member.account_id=OLD.created_by_account_id
        AND owner_member.role_code='owner' AND owner_member.status='active')
    AND EXISTS(SELECT 1 FROM workspace_memberships projected_member
      WHERE projected_member.workspace_id=OLD.workspace_id
        AND projected_member.account_id=NEW.created_by_account_id
        AND projected_member.role_code!='owner' AND projected_member.status='active')
    THEN RAISE(IGNORE) ELSE RAISE(ABORT,'workspace_owner_immutable') END;
END;

DROP TRIGGER trg_invitation_identity_bindings_update_prd092;
DROP TRIGGER trg_invitation_identity_bindings_delete_prd092;
ALTER TABLE invitation_identity_bindings RENAME TO invitation_identity_bindings_prd092;
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
  status TEXT NOT NULL CHECK(status IN('verified_pending_membership','consumed')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision BETWEEN 1 AND 2),
  verified_at TEXT NOT NULL CHECK(unixepoch(verified_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,invitation_id) REFERENCES invitations(tenant_id,invitation_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(registration_intent_id) REFERENCES registration_intents(registration_intent_id),
  CHECK((status='verified_pending_membership' AND revision=1 AND consumed_at IS NULL)
    OR(status='consumed' AND revision=2 AND consumed_at IS NOT NULL))
);
INSERT INTO invitation_identity_bindings(
  invitation_identity_binding_id,invitation_id,tenant_id,account_id,
  registration_intent_id,email_fingerprint,status,revision,verified_at,consumed_at)
SELECT invitation_identity_binding_id,invitation_id,tenant_id,account_id,
  registration_intent_id,email_fingerprint,status,revision,verified_at,NULL
FROM invitation_identity_bindings_prd092;
DROP TABLE invitation_identity_bindings_prd092;
CREATE TRIGGER trg_invitation_identity_bindings_update_prd094
BEFORE UPDATE ON invitation_identity_bindings FOR EACH ROW
WHEN NEW.invitation_identity_binding_id!=OLD.invitation_identity_binding_id
  OR NEW.invitation_id!=OLD.invitation_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id OR NEW.registration_intent_id!=OLD.registration_intent_id
  OR NEW.email_fingerprint!=OLD.email_fingerprint OR NEW.verified_at!=OLD.verified_at
  OR OLD.status!='verified_pending_membership' OR NEW.status!='consumed'
  OR OLD.revision!=1 OR NEW.revision!=2 OR NEW.consumed_at IS NULL
BEGIN SELECT RAISE(ABORT,'invitation_identity_binding_immutable'); END;
CREATE TRIGGER trg_invitation_identity_bindings_delete_prd094
BEFORE DELETE ON invitation_identity_bindings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'invitation_identity_binding_immutable'); END;

CREATE TABLE membership_invitations(
  membership_invitation_id TEXT PRIMARY KEY CHECK(length(membership_invitation_id)=36
    AND substr(membership_invitation_id,1,4)='min_'
    AND substr(membership_invitation_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  legacy_invitation_id TEXT NOT NULL UNIQUE,
  target_email_hmac TEXT NOT NULL CHECK(length(target_email_hmac)=64
    AND target_email_hmac NOT GLOB '*[^0-9a-f]*'),
  target_role_code TEXT NOT NULL CHECK(target_role_code IN(
    'admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  inviter_membership_id TEXT NOT NULL,
  inviter_membership_revision INTEGER NOT NULL CHECK(inviter_membership_revision>=1),
  token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash)=64
    AND token_hash NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('pending','accepted','expired','cancelled')),
  target_account_id TEXT,
  accepted_membership_id TEXT,
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  accepted_at TEXT CHECK(accepted_at IS NULL OR unixepoch(accepted_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(tenant_id,legacy_invitation_id) REFERENCES invitations(tenant_id,invitation_id),
  FOREIGN KEY(inviter_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  FOREIGN KEY(target_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(accepted_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  CHECK((status='accepted' AND target_account_id IS NOT NULL
      AND accepted_membership_id IS NOT NULL AND accepted_at IS NOT NULL)
    OR(status!='accepted' AND accepted_membership_id IS NULL AND accepted_at IS NULL))
);
CREATE INDEX idx_membership_invitations_workspace_prd094
  ON membership_invitations(workspace_id,status,created_at,membership_invitation_id);

CREATE TABLE workspace_sessions(
  workspace_session_id TEXT PRIMARY KEY CHECK(length(workspace_session_id)=36
    AND substr(workspace_session_id,1,4)='wss_'
    AND substr(workspace_session_id,5) NOT GLOB '*[^0-9a-f]*'),
  global_session_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  membership_id TEXT NOT NULL,
  token_hmac TEXT NOT NULL UNIQUE CHECK(length(token_hmac)=64
    AND token_hmac NOT GLOB '*[^0-9a-f]*'),
  csrf_hmac TEXT NOT NULL CHECK(length(csrf_hmac)=64
    AND csrf_hmac NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('active','revoked','expired')),
  membership_revision INTEGER NOT NULL CHECK(membership_revision>=1),
  workspace_revision INTEGER NOT NULL CHECK(workspace_revision>=1),
  account_session_revision INTEGER NOT NULL CHECK(account_session_revision>=1),
  legacy_permission_revision INTEGER NOT NULL CHECK(legacy_permission_revision>=1),
  matrix_fingerprint TEXT NOT NULL CHECK(length(matrix_fingerprint)=64
    AND matrix_fingerprint NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  last_seen_at TEXT NOT NULL CHECK(unixepoch(last_seen_at) IS NOT NULL),
  absolute_expires_at TEXT NOT NULL CHECK(unixepoch(absolute_expires_at) IS NOT NULL),
  idle_expires_at TEXT NOT NULL CHECK(unixepoch(idle_expires_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  revoked_reason TEXT CHECK(revoked_reason IS NULL OR(length(revoked_reason) BETWEEN 2 AND 80
    AND revoked_reason=lower(revoked_reason)
    AND revoked_reason NOT GLOB '*[^a-z0-9._]*')),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(global_session_id) REFERENCES global_sessions(session_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  UNIQUE(global_session_id,workspace_session_id),
  CHECK((status='active' AND revoked_at IS NULL AND revoked_reason IS NULL)
    OR(status='revoked' AND revoked_at IS NOT NULL AND revoked_reason IS NOT NULL)
    OR status='expired')
);
CREATE INDEX idx_workspace_sessions_member_prd094
  ON workspace_sessions(membership_id,status,created_at,workspace_session_id);
CREATE INDEX idx_workspace_sessions_global_prd094
  ON workspace_sessions(global_session_id,status,created_at,workspace_session_id);

CREATE TABLE owner_transfers(
  owner_transfer_id TEXT PRIMARY KEY CHECK(length(owner_transfer_id)=36
    AND substr(owner_transfer_id,1,4)='owt_'
    AND substr(owner_transfer_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  source_membership_id TEXT NOT NULL,
  target_membership_id TEXT NOT NULL,
  source_membership_revision INTEGER NOT NULL CHECK(source_membership_revision>=1),
  target_membership_revision INTEGER NOT NULL CHECK(target_membership_revision>=1),
  workspace_revision INTEGER NOT NULL CHECK(workspace_revision>=1),
  status TEXT NOT NULL CHECK(status IN(
    'pending','completed','cancelled','expired','superseded')),
  reauthenticated_at TEXT NOT NULL CHECK(unixepoch(reauthenticated_at) IS NOT NULL),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9._]*'),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(source_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  FOREIGN KEY(target_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  CHECK(source_membership_id!=target_membership_id),
  CHECK((status='completed' AND completed_at IS NOT NULL)
    OR(status!='completed' AND completed_at IS NULL))
);
CREATE UNIQUE INDEX uq_owner_transfers_pending_prd094
  ON owner_transfers(workspace_id) WHERE status='pending';

CREATE TABLE membership_events(
  membership_event_id TEXT PRIMARY KEY CHECK(length(membership_event_id)=36
    AND substr(membership_event_id,1,4)='mev_'
    AND substr(membership_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_account_id TEXT NOT NULL,
  subject_membership_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'membership.created','membership.updated','membership.disabled','membership.invited',
    'invitation.accepted','workspace.switched','workspace.owner_transferred')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9._]*'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=2048),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(subject_membership_id) REFERENCES workspace_memberships(workspace_membership_id)
);
CREATE TABLE membership_event_outbox(
  membership_event_outbox_id TEXT PRIMARY KEY CHECK(length(membership_event_outbox_id)=36
    AND substr(membership_event_outbox_id,1,4)='mob_'
    AND substr(membership_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  membership_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 100),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(membership_event_id) REFERENCES membership_events(membership_event_id)
);
CREATE TRIGGER trg_membership_events_update_prd094
BEFORE UPDATE ON membership_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'membership_event_immutable'); END;
CREATE TRIGGER trg_membership_events_delete_prd094
BEFORE DELETE ON membership_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'membership_event_immutable'); END;

CREATE TRIGGER trg_workspace_memberships_insert_prd094
BEFORE INSERT ON workspace_memberships FOR EACH ROW
WHEN NEW.revision!=1
  OR (NEW.role_code!='service_account' AND NOT EXISTS(
    SELECT 1 FROM account_identity_links link
    WHERE link.account_id=NEW.account_id AND link.legacy_tenant_id=NEW.tenant_id
      AND link.legacy_user_id=NEW.legacy_user_id AND link.status='active'))
BEGIN SELECT RAISE(ABORT,'workspace_membership_invalid'); END;
CREATE TRIGGER trg_workspace_memberships_update_prd094
BEFORE UPDATE ON workspace_memberships FOR EACH ROW
WHEN NEW.workspace_membership_id!=OLD.workspace_membership_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id OR NEW.legacy_user_id IS NOT OLD.legacy_user_id
  OR NEW.source!=OLD.source OR NEW.created_by_account_id IS NOT OLD.created_by_account_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR ((OLD.role_code='owner' OR NEW.role_code='owner') AND NOT EXISTS(
    SELECT 1 FROM owner_transfers transfer
    WHERE transfer.workspace_id=OLD.workspace_id AND transfer.status='pending'
      AND (transfer.source_membership_id=OLD.workspace_membership_id
        OR transfer.target_membership_id=OLD.workspace_membership_id)))
  OR (OLD.role_code='owner' AND (NEW.role_code!='owner' OR NEW.status!='active')
    AND (SELECT COUNT(*) FROM workspace_memberships member
      WHERE member.workspace_id=OLD.workspace_id
        AND member.role_code='owner' AND member.status='active')<=1)
BEGIN SELECT RAISE(ABORT,'workspace_membership_invalid'); END;
CREATE TRIGGER trg_workspace_memberships_delete_prd094
BEFORE DELETE ON workspace_memberships FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_membership_immutable'); END;
CREATE TRIGGER trg_workspace_memberships_revoke_sessions_prd094
AFTER UPDATE OF role_code,status,revision ON workspace_memberships FOR EACH ROW
BEGIN
  UPDATE workspace_sessions SET status='revoked',revoked_at=NEW.updated_at,
    revoked_reason='membership_changed',updated_at=NEW.updated_at
  WHERE membership_id=NEW.workspace_membership_id AND status='active';
END;
CREATE TRIGGER trg_account_links_membership_backfill_prd094
AFTER INSERT ON account_identity_links FOR EACH ROW
WHEN NEW.source_type='legacy_user' AND NEW.status='active'
  AND NOT EXISTS(SELECT 1 FROM workspace_user_projection_claims claim
    WHERE claim.tenant_id=NEW.legacy_tenant_id AND claim.legacy_user_id=NEW.legacy_user_id
      AND claim.status='pending')
  AND NOT EXISTS(
    SELECT 1
    FROM membership_invitations membership_invitation
    JOIN invitations invitation
      ON invitation.tenant_id=membership_invitation.tenant_id
     AND invitation.invitation_id=membership_invitation.legacy_invitation_id
    JOIN users invited_user
      ON invited_user.tenant_id=invitation.tenant_id
     AND invited_user.user_id=NEW.legacy_user_id
    WHERE membership_invitation.status='pending' AND invitation.status='pending'
      AND invitation.tenant_id=NEW.legacy_tenant_id
      AND lower(invitation.email)=lower(invited_user.email))
BEGIN
  INSERT OR IGNORE INTO workspace_memberships(
    workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
    role_code,status,source,revision,created_by_account_id,updated_by_account_id,
    created_at,updated_at)
  SELECT 'wmb_'||lower(hex(randomblob(16))),binding.workspace_id,NEW.legacy_tenant_id,
    NEW.account_id,NEW.legacy_user_id,
    CASE WHEN user.role='admin' AND user.status='active'
          AND NOT EXISTS(SELECT 1 FROM workspace_memberships current
            WHERE current.workspace_id=binding.workspace_id)
          AND workspace.creation_source!='user_created' THEN 'owner'
      WHEN user.role IN('admin','ops_admin') THEN 'admin'
      WHEN user.role='developer' THEN 'developer'
      WHEN user.role='security_admin' THEN 'security_admin'
      WHEN user.role='finance_admin' THEN 'finance_admin'
      ELSE 'viewer' END,
    CASE WHEN user.status='active' THEN 'active' ELSE 'disabled' END,
    'legacy_backfill',1,NEW.account_id,NEW.account_id,user.created_at,user.updated_at
  FROM workspace_tenant_bindings binding
  JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
  JOIN users user ON user.tenant_id=NEW.legacy_tenant_id
    AND user.user_id=NEW.legacy_user_id
  WHERE binding.tenant_id=NEW.legacy_tenant_id
    AND workspace.workspace_id NOT IN(
      SELECT conflict.workspace_id FROM workspace_membership_migration_conflicts conflict)
    AND NOT EXISTS(SELECT 1 FROM workspace_memberships current
      WHERE current.workspace_id=binding.workspace_id AND current.account_id=NEW.account_id);
END;
CREATE TRIGGER trg_global_sessions_revoke_workspace_prd094
AFTER UPDATE OF status ON global_sessions FOR EACH ROW
WHEN OLD.status='active' AND NEW.status!='active'
BEGIN
  UPDATE workspace_sessions SET status='revoked',revoked_at=NEW.updated_at,
    revoked_reason='global_session_revoked',updated_at=NEW.updated_at
  WHERE global_session_id=NEW.session_id AND status='active';
END;

CREATE TRIGGER trg_membership_invitations_update_prd094
BEFORE UPDATE ON membership_invitations FOR EACH ROW
WHEN NEW.membership_invitation_id!=OLD.membership_invitation_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.legacy_invitation_id!=OLD.legacy_invitation_id
  OR NEW.target_email_hmac!=OLD.target_email_hmac
  OR NEW.target_role_code!=OLD.target_role_code
  OR NEW.inviter_membership_id!=OLD.inviter_membership_id
  OR NEW.inviter_membership_revision!=OLD.inviter_membership_revision
  OR NEW.token_hash!=OLD.token_hash OR NEW.expires_at!=OLD.expires_at
  OR NEW.created_at!=OLD.created_at OR OLD.status!='pending'
  OR NEW.status NOT IN('accepted','expired','cancelled')
BEGIN SELECT RAISE(ABORT,'membership_invitation_immutable'); END;
CREATE TRIGGER trg_membership_invitations_delete_prd094
BEFORE DELETE ON membership_invitations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'membership_invitation_immutable'); END;
CREATE TRIGGER trg_owner_transfers_update_prd094
BEFORE UPDATE ON owner_transfers FOR EACH ROW
WHEN NEW.owner_transfer_id!=OLD.owner_transfer_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.source_membership_id!=OLD.source_membership_id
  OR NEW.target_membership_id!=OLD.target_membership_id
  OR NEW.source_membership_revision!=OLD.source_membership_revision
  OR NEW.target_membership_revision!=OLD.target_membership_revision
  OR NEW.workspace_revision!=OLD.workspace_revision
  OR NEW.reauthenticated_at!=OLD.reauthenticated_at
  OR NEW.request_id!=OLD.request_id OR NEW.reason_code!=OLD.reason_code
  OR NEW.created_at!=OLD.created_at OR OLD.status!='pending'
  OR NEW.status NOT IN('completed','cancelled','expired','superseded')
BEGIN SELECT RAISE(ABORT,'owner_transfer_immutable'); END;
CREATE TRIGGER trg_owner_transfers_delete_prd094
BEFORE DELETE ON owner_transfers FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'owner_transfer_immutable'); END;

CREATE TEMP TABLE prd094_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd094_up_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM workspaces workspace
    WHERE workspace.status='active' AND NOT EXISTS(
      SELECT 1 FROM workspace_memberships member
      WHERE member.workspace_id=workspace.workspace_id
        AND member.role_code='owner' AND member.status='active'))
  + (SELECT COUNT(*) FROM workspace_memberships member
      LEFT JOIN account_identity_links link ON link.account_id=member.account_id
        AND link.legacy_tenant_id=member.tenant_id AND link.legacy_user_id=member.legacy_user_id
      WHERE member.role_code!='service_account' AND link.identity_link_id IS NULL)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd094_up_final_guard;

PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
