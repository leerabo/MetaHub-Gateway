-- metahub: workspace-private-gateway-capability-v1
-- Private Gateway is a SaaS-controlled organization capability. New and
-- existing organizations remain disabled until a platform operator enables it.
CREATE TABLE workspace_private_gateway_capabilities(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN(0,1)),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  updated_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(updated_by_operator_id) REFERENCES platform_operators(operator_id)
);
CREATE INDEX idx_workspace_private_gateway_capabilities_enabled
  ON workspace_private_gateway_capabilities(enabled,updated_at,workspace_id);

INSERT INTO workspace_private_gateway_capabilities(
  workspace_id,tenant_id,enabled,revision,updated_by_operator_id,created_at,updated_at)
SELECT workspace_id,tenant_id,0,1,NULL,datetime('now'),datetime('now')
FROM workspace_tenant_bindings
WHERE status='active';
