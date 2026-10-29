-- Migration 0184 rebuilt preferences by renaming its table. SQLite rewrote
-- this trigger on workspace_memberships to the temporary name, then kept the
-- reference after that table was dropped. Repair only the trigger: existing
-- memberships, preferences, sessions, keys and audit rows are preserved.
DROP TRIGGER IF EXISTS trg_workspace_membership_revoke_navigation_prd146;
CREATE TRIGGER trg_workspace_membership_revoke_navigation_prd146
AFTER UPDATE OF status,revision ON workspace_memberships FOR EACH ROW
WHEN NEW.status!='active' OR NEW.revision!=OLD.revision
BEGIN
  UPDATE workspace_navigation_preferences SET status='membership_revoked',revision=revision+1,
    updated_at=NEW.updated_at
  WHERE workspace_membership_id=NEW.workspace_membership_id AND status='active';
  DELETE FROM workspace_navigation_member_states
  WHERE workspace_membership_id=NEW.workspace_membership_id;
END;
