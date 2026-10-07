-- metahub: application-memberships-v1
-- An application membership grants an organization member access to an application.
-- It is separate from virtual_key bindings so assigning a member does not rotate or
-- silently rebind the member's existing key.
CREATE TABLE application_memberships(
  application_membership_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  application_id TEXT NOT NULL,
  workspace_membership_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(application_id) REFERENCES applications(application_id),
  FOREIGN KEY(workspace_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  FOREIGN KEY(created_by) REFERENCES users(user_id),
  UNIQUE(tenant_id, application_id, workspace_membership_id)
);

CREATE INDEX idx_application_memberships_application
  ON application_memberships(tenant_id, application_id, status, workspace_membership_id);
CREATE INDEX idx_application_memberships_member
  ON application_memberships(tenant_id, workspace_membership_id, status, application_id);
