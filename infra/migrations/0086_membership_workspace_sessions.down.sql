-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd094_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd094_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0086' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM workspace_sessions)
  + (SELECT COUNT(*) FROM membership_invitations)
  + (SELECT COUNT(*) FROM owner_transfers)
  + (SELECT COUNT(*) FROM membership_events)
  + (SELECT COUNT(*) FROM invitation_identity_bindings WHERE status!='verified_pending_membership'
      OR revision!=1 OR consumed_at IS NOT NULL)
  + (SELECT COUNT(*) FROM workspace_memberships WHERE revision!=1
      OR source='membership_invitation')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd094_down_guard;

DROP TRIGGER trg_global_sessions_revoke_workspace_prd094;
DROP TRIGGER trg_account_links_membership_backfill_prd094;
DROP TRIGGER trg_workspace_memberships_revoke_sessions_prd094;
DROP TRIGGER trg_workspace_memberships_delete_prd094;
DROP TRIGGER trg_workspace_memberships_update_prd094;
DROP TRIGGER trg_workspace_memberships_insert_prd094;
DROP TRIGGER trg_membership_invitations_delete_prd094;
DROP TRIGGER trg_membership_invitations_update_prd094;
DROP TRIGGER trg_owner_transfers_delete_prd094;
DROP TRIGGER trg_owner_transfers_update_prd094;
DROP TRIGGER trg_membership_events_delete_prd094;
DROP TRIGGER trg_membership_events_update_prd094;

DROP TABLE membership_event_outbox;
DROP TABLE membership_events;
DROP TABLE owner_transfers;
DROP TABLE workspace_sessions;
DROP TABLE membership_invitations;

DROP TRIGGER trg_invitation_identity_bindings_update_prd094;
DROP TRIGGER trg_invitation_identity_bindings_delete_prd094;
ALTER TABLE invitation_identity_bindings RENAME TO invitation_identity_bindings_prd094;
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
  FOREIGN KEY(registration_intent_id) REFERENCES registration_intents(registration_intent_id)
);
INSERT INTO invitation_identity_bindings(
  invitation_identity_binding_id,invitation_id,tenant_id,account_id,
  registration_intent_id,email_fingerprint,status,revision,verified_at)
SELECT invitation_identity_binding_id,invitation_id,tenant_id,account_id,
  registration_intent_id,email_fingerprint,status,revision,verified_at
FROM invitation_identity_bindings_prd094;
DROP TABLE invitation_identity_bindings_prd094;
CREATE TRIGGER trg_invitation_identity_bindings_update_prd092
BEFORE UPDATE ON invitation_identity_bindings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'invitation_identity_binding_immutable'); END;
CREATE TRIGGER trg_invitation_identity_bindings_delete_prd092
BEFORE DELETE ON invitation_identity_bindings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'invitation_identity_binding_immutable'); END;

ALTER TABLE workspace_memberships RENAME TO workspace_memberships_prd094;
CREATE TABLE workspace_memberships(
  workspace_membership_id TEXT PRIMARY KEY CHECK(length(workspace_membership_id)=36
    AND substr(workspace_membership_id,1,4)='wmb_'
    AND substr(workspace_membership_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  legacy_user_id TEXT NOT NULL,
  role_code TEXT NOT NULL CHECK(role_code='owner'),
  status TEXT NOT NULL CHECK(status='active'),
  source TEXT NOT NULL CHECK(source IN('workspace_creator','legacy_backfill')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(tenant_id,legacy_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(workspace_id,account_id),
  UNIQUE(tenant_id,legacy_user_id)
);
INSERT INTO workspace_memberships(
  workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
  role_code,status,source,revision,created_at,updated_at)
SELECT workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
  'owner','active',source,1,created_at,updated_at
FROM workspace_memberships_prd094 WHERE role_code='owner' AND status='active';
DROP TABLE workspace_memberships_prd094;
CREATE INDEX idx_workspace_memberships_account_prd093
  ON workspace_memberships(account_id,status,created_at,workspace_id);
CREATE TRIGGER trg_workspace_memberships_update_prd093
BEFORE UPDATE ON workspace_memberships FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_membership_foundation_immutable'); END;
CREATE TRIGGER trg_workspace_memberships_delete_prd093
BEFORE DELETE ON workspace_memberships FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_membership_foundation_immutable'); END;

DROP TRIGGER trg_workspace_membership_conflicts_update_prd094;
DROP TRIGGER trg_workspace_membership_conflicts_delete_prd094;
UPDATE workspaces
SET status=(SELECT conflict.previous_workspace_status
      FROM workspace_membership_migration_conflicts conflict
      WHERE conflict.workspace_id=workspaces.workspace_id),
    profile_state=(SELECT conflict.previous_profile_state
      FROM workspace_membership_migration_conflicts conflict
      WHERE conflict.workspace_id=workspaces.workspace_id),
    revision=(SELECT conflict.previous_revision
      FROM workspace_membership_migration_conflicts conflict
      WHERE conflict.workspace_id=workspaces.workspace_id),
    updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
WHERE workspace_id IN(SELECT workspace_id FROM workspace_membership_migration_conflicts);
DROP TABLE workspace_membership_migration_conflicts;

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
BEGIN SELECT RAISE(ABORT,'workspace_owner_immutable'); END;

CREATE TEMP TABLE prd094_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd094_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM workspaces workspace WHERE NOT EXISTS(
    SELECT 1 FROM workspace_memberships member
    WHERE member.workspace_id=workspace.workspace_id
      AND member.role_code='owner' AND member.status='active'))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd094_down_final_guard;

PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
