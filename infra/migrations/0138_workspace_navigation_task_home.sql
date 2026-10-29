-- PRD-146 authoritative Workspace navigation, task home, preferences, and tools.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd146_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd146_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0137' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_tenant_bindings','workspace_memberships','global_accounts',
      'user_preferences','account_identity_links','console_experience_scheduler_state'))=6
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd146_up_guard;

CREATE TABLE workspace_navigation_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'workspace.navigation.read','workspace.task_home.read',
    'workspace.advanced_tool.read','workspace.advanced_tool.open')),
  introduced_in_migration TEXT NOT NULL CHECK(introduced_in_migration='0138')
);
INSERT INTO workspace_navigation_permission_extensions VALUES
  ('workspace.navigation.read','0138'),
  ('workspace.task_home.read','0138'),
  ('workspace.advanced_tool.read','0138'),
  ('workspace.advanced_tool.open','0138');

CREATE TABLE workspace_navigation_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code) REFERENCES workspace_navigation_permission_extensions(permission_code)
);
INSERT INTO workspace_navigation_role_permissions(role_code,permission_code)
SELECT role_code,permission_code FROM (
  SELECT 'owner' role_code UNION ALL SELECT 'admin' UNION ALL SELECT 'developer'
  UNION ALL SELECT 'security_admin' UNION ALL SELECT 'finance_admin'
  UNION ALL SELECT 'legal_admin' UNION ALL SELECT 'auditor' UNION ALL SELECT 'viewer') roles
CROSS JOIN (
  SELECT 'workspace.navigation.read' permission_code
  UNION ALL SELECT 'workspace.task_home.read') base;
INSERT INTO workspace_navigation_role_permissions(role_code,permission_code)
SELECT role_code,permission_code FROM (
  SELECT 'owner' role_code UNION ALL SELECT 'admin' UNION ALL SELECT 'developer'
  UNION ALL SELECT 'security_admin' UNION ALL SELECT 'auditor') roles
CROSS JOIN (
  SELECT 'workspace.advanced_tool.read' permission_code
  UNION ALL SELECT 'workspace.advanced_tool.open') advanced;

CREATE TRIGGER trg_workspace_navigation_permission_insert_prd146
BEFORE INSERT ON workspace_navigation_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_navigation_permissions_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_permission_update_prd146
BEFORE UPDATE ON workspace_navigation_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_navigation_permissions_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_permission_delete_prd146
BEFORE DELETE ON workspace_navigation_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_navigation_permissions_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_role_insert_prd146
BEFORE INSERT ON workspace_navigation_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_navigation_roles_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_role_update_prd146
BEFORE UPDATE ON workspace_navigation_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_navigation_roles_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_role_delete_prd146
BEFORE DELETE ON workspace_navigation_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_navigation_roles_immutable'); END;

CREATE TABLE workspace_navigation_contract_snapshots(
  navigation_contract_snapshot_id TEXT PRIMARY KEY CHECK(
    length(navigation_contract_snapshot_id)=36 AND substr(navigation_contract_snapshot_id,1,4)='ncs_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  contract_version TEXT NOT NULL CHECK(contract_version='workspace-navigation-v1'),
  source_catalog_version TEXT NOT NULL CHECK(source_catalog_version='console-navigation-0137'),
  source_catalog_sha256 TEXT NOT NULL CHECK(length(source_catalog_sha256)=64),
  disposition_sha256 TEXT NOT NULL CHECK(length(disposition_sha256)=64),
  extra_sha256 TEXT NOT NULL CHECK(length(extra_sha256)=64),
  item_count INTEGER NOT NULL CHECK(item_count=68),
  status TEXT NOT NULL CHECK(status IN('candidate','active','superseded','rejected')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  generation INTEGER NOT NULL CHECK(generation>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(workspace_id,tenant_id,contract_version,generation),
  UNIQUE(workspace_id,tenant_id,navigation_contract_snapshot_id,generation),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE TABLE workspace_navigation_contract_items(
  navigation_contract_item_id TEXT PRIMARY KEY CHECK(
    length(navigation_contract_item_id)=36 AND substr(navigation_contract_item_id,1,4)='nci_'),
  navigation_contract_snapshot_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  contract_generation INTEGER NOT NULL CHECK(contract_generation>=1),
  source_navigation_id TEXT NOT NULL CHECK(length(source_navigation_id) BETWEEN 2 AND 80),
  source_kind TEXT NOT NULL CHECK(source_kind IN('excel','extra')),
  disposition_code TEXT CHECK(disposition_code IS NULL OR disposition_code GLOB 'D[0-2][0-9]'),
  placement TEXT NOT NULL CHECK(placement IN(
    'primary','detail','advanced','account','developer_center','task_home','replaced','retired_internal')),
  route_code TEXT NOT NULL CHECK(length(route_code) BETWEEN 3 AND 120),
  group_code TEXT CHECK(group_code IS NULL OR group_code IN(
    'home','workspace','gateway','finance','governance','applications','observability','developer-center')),
  required_permissions_json TEXT NOT NULL CHECK(json_valid(required_permissions_json)
    AND json_type(required_permissions_json)='array' AND json(required_permissions_json)=required_permissions_json),
  display_order INTEGER NOT NULL CHECK(display_order BETWEEN 0 AND 1000),
  item_sha256 TEXT NOT NULL CHECK(length(item_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(navigation_contract_snapshot_id,source_navigation_id),
  FOREIGN KEY(workspace_id,tenant_id,navigation_contract_snapshot_id,contract_generation)
    REFERENCES workspace_navigation_contract_snapshots(
      workspace_id,tenant_id,navigation_contract_snapshot_id,generation),
  CHECK((source_kind='excel' AND disposition_code IS NOT NULL)
    OR(source_kind='extra' AND disposition_code IS NULL))
);
CREATE TABLE workspace_navigation_contract_heads(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  navigation_contract_snapshot_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL CHECK(generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id,navigation_contract_snapshot_id,generation)
    REFERENCES workspace_navigation_contract_snapshots(
      workspace_id,tenant_id,navigation_contract_snapshot_id,generation)
);

CREATE TABLE workspace_navigation_projection_versions(
  workspace_navigation_projection_id TEXT PRIMARY KEY CHECK(
    length(workspace_navigation_projection_id)=36 AND substr(workspace_navigation_projection_id,1,4)='wnp_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  navigation_contract_snapshot_id TEXT NOT NULL,
  contract_generation INTEGER NOT NULL CHECK(contract_generation>=1),
  entitlement_generation INTEGER NOT NULL CHECK(entitlement_generation>=1),
  status TEXT NOT NULL CHECK(status IN('ready','partial','stale')),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  generation INTEGER NOT NULL CHECK(generation>=1),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(workspace_id,tenant_id,generation),
  UNIQUE(workspace_id,tenant_id,workspace_navigation_projection_id,generation),
  FOREIGN KEY(workspace_id,tenant_id,navigation_contract_snapshot_id,contract_generation)
    REFERENCES workspace_navigation_contract_snapshots(
      workspace_id,tenant_id,navigation_contract_snapshot_id,generation)
);
CREATE TABLE workspace_navigation_projection_heads(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_navigation_projection_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL CHECK(generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id,workspace_navigation_projection_id,generation)
    REFERENCES workspace_navigation_projection_versions(
      workspace_id,tenant_id,workspace_navigation_projection_id,generation)
);
CREATE TABLE workspace_navigation_projection_items(
  workspace_navigation_projection_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  projection_generation INTEGER NOT NULL CHECK(projection_generation>=1),
  source_navigation_id TEXT NOT NULL,
  route_code TEXT NOT NULL,
  placement TEXT NOT NULL CHECK(placement IN(
    'primary','detail','advanced','account','developer_center','task_home')),
  group_code TEXT,
  required_permissions_json TEXT NOT NULL CHECK(json_valid(required_permissions_json)),
  display_order INTEGER NOT NULL CHECK(display_order BETWEEN 0 AND 1000),
  item_sha256 TEXT NOT NULL CHECK(length(item_sha256)=64),
  PRIMARY KEY(workspace_navigation_projection_id,source_navigation_id),
  FOREIGN KEY(workspace_id,tenant_id,workspace_navigation_projection_id,projection_generation)
    REFERENCES workspace_navigation_projection_versions(
      workspace_id,tenant_id,workspace_navigation_projection_id,generation)
);
CREATE TABLE workspace_navigation_projection_sources(
  workspace_navigation_projection_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  projection_generation INTEGER NOT NULL CHECK(projection_generation>=1),
  source_code TEXT NOT NULL CHECK(source_code IN('catalog','permissions','entitlements','deployment')),
  source_revision INTEGER NOT NULL CHECK(source_revision>=0),
  source_watermark TEXT NOT NULL CHECK(length(source_watermark) BETWEEN 1 AND 160),
  freshness TEXT NOT NULL CHECK(freshness IN('fresh','stale','unavailable')),
  PRIMARY KEY(workspace_navigation_projection_id,source_code),
  FOREIGN KEY(workspace_id,tenant_id,workspace_navigation_projection_id,projection_generation)
    REFERENCES workspace_navigation_projection_versions(
      workspace_id,tenant_id,workspace_navigation_projection_id,generation)
);

CREATE TRIGGER trg_workspace_navigation_contract_update_prd146
BEFORE UPDATE ON workspace_navigation_contract_snapshots
BEGIN SELECT RAISE(ABORT,'workspace_navigation_contract_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_contract_delete_prd146
BEFORE DELETE ON workspace_navigation_contract_snapshots
BEGIN SELECT RAISE(ABORT,'workspace_navigation_contract_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_item_update_prd146
BEFORE UPDATE ON workspace_navigation_contract_items
BEGIN SELECT RAISE(ABORT,'workspace_navigation_item_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_item_delete_prd146
BEFORE DELETE ON workspace_navigation_contract_items
BEGIN SELECT RAISE(ABORT,'workspace_navigation_item_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_projection_update_prd146
BEFORE UPDATE ON workspace_navigation_projection_versions
BEGIN SELECT RAISE(ABORT,'workspace_navigation_projection_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_projection_delete_prd146
BEFORE DELETE ON workspace_navigation_projection_versions
BEGIN SELECT RAISE(ABORT,'workspace_navigation_projection_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_projection_item_update_prd146
BEFORE UPDATE ON workspace_navigation_projection_items
BEGIN SELECT RAISE(ABORT,'workspace_navigation_projection_item_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_projection_item_delete_prd146
BEFORE DELETE ON workspace_navigation_projection_items
BEGIN SELECT RAISE(ABORT,'workspace_navigation_projection_item_immutable'); END;

CREATE TABLE workspace_role_task_home_versions(
  role_task_home_version_id TEXT PRIMARY KEY CHECK(
    length(role_task_home_version_id)=36 AND substr(role_task_home_version_id,1,4)='rth_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_membership_id TEXT NOT NULL,
  membership_revision INTEGER NOT NULL CHECK(membership_revision>=1),
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  contract_snapshot_id TEXT NOT NULL,
  contract_generation INTEGER NOT NULL CHECK(contract_generation>=1),
  source_digest TEXT NOT NULL CHECK(length(source_digest)=64),
  status TEXT NOT NULL CHECK(status IN('ready','partial','stale')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(workspace_membership_id,generation),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(workspace_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  FOREIGN KEY(workspace_id,tenant_id,contract_snapshot_id,contract_generation)
    REFERENCES workspace_navigation_contract_snapshots(
      workspace_id,tenant_id,navigation_contract_snapshot_id,generation)
);
CREATE TABLE workspace_role_task_home_tasks(
  role_task_home_version_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_membership_id TEXT NOT NULL,
  task_code TEXT NOT NULL CHECK(length(task_code) BETWEEN 3 AND 80),
  source_code TEXT NOT NULL CHECK(length(source_code) BETWEEN 3 AND 80),
  source_revision INTEGER NOT NULL CHECK(source_revision>=0),
  evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),
  status TEXT NOT NULL CHECK(status IN(
    'not_started','ready','blocked','in_progress','completed','needs_attention','not_applicable')),
  priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 1000),
  action_route_code TEXT,
  completion_generation INTEGER NOT NULL CHECK(completion_generation>=0),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(role_task_home_version_id,task_code),
  FOREIGN KEY(role_task_home_version_id) REFERENCES workspace_role_task_home_versions(role_task_home_version_id),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(workspace_membership_id) REFERENCES workspace_memberships(workspace_membership_id)
);
CREATE TABLE workspace_role_task_home_heads(
  workspace_membership_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  role_task_home_version_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL CHECK(generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  FOREIGN KEY(role_task_home_version_id) REFERENCES workspace_role_task_home_versions(role_task_home_version_id),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE TABLE workspace_role_task_home_sources(
  role_task_home_version_id TEXT NOT NULL,
  source_code TEXT NOT NULL,
  source_revision INTEGER NOT NULL CHECK(source_revision>=0),
  source_watermark TEXT NOT NULL CHECK(length(source_watermark) BETWEEN 1 AND 160),
  evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),
  freshness TEXT NOT NULL CHECK(freshness IN('fresh','stale','unavailable')),
  PRIMARY KEY(role_task_home_version_id,source_code),
  FOREIGN KEY(role_task_home_version_id) REFERENCES workspace_role_task_home_versions(role_task_home_version_id)
);
CREATE TRIGGER trg_workspace_role_task_version_update_prd146
BEFORE UPDATE ON workspace_role_task_home_versions
BEGIN SELECT RAISE(ABORT,'workspace_role_task_version_immutable'); END;
CREATE TRIGGER trg_workspace_role_task_version_delete_prd146
BEFORE DELETE ON workspace_role_task_home_versions
BEGIN SELECT RAISE(ABORT,'workspace_role_task_version_immutable'); END;
CREATE TRIGGER trg_workspace_role_task_update_prd146
BEFORE UPDATE ON workspace_role_task_home_tasks
BEGIN SELECT RAISE(ABORT,'workspace_role_task_immutable'); END;
CREATE TRIGGER trg_workspace_role_task_delete_prd146
BEFORE DELETE ON workspace_role_task_home_tasks
BEGIN SELECT RAISE(ABORT,'workspace_role_task_immutable'); END;

CREATE TABLE workspace_navigation_preferences(
  navigation_preference_id TEXT PRIMARY KEY CHECK(
    length(navigation_preference_id)=36 AND substr(navigation_preference_id,1,4)='nvp_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  workspace_membership_id TEXT NOT NULL,
  membership_revision INTEGER NOT NULL CHECK(membership_revision>=1),
  last_primary_route_code TEXT,
  expanded_group_code TEXT CHECK(expanded_group_code IS NULL OR expanded_group_code IN(
    'home','workspace','gateway','finance','governance','applications','observability','developer-center')),
  sidebar_collapsed INTEGER NOT NULL CHECK(sidebar_collapsed IN(0,1)),
  contract_snapshot_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('active','membership_revoked')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(workspace_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  FOREIGN KEY(contract_snapshot_id) REFERENCES workspace_navigation_contract_snapshots(navigation_contract_snapshot_id)
);
CREATE UNIQUE INDEX uq_workspace_navigation_preference_active_prd146
  ON workspace_navigation_preferences(workspace_id,tenant_id,account_id) WHERE status='active';
CREATE TABLE workspace_navigation_preference_commands(
  navigation_preference_command_id TEXT PRIMARY KEY CHECK(
    length(navigation_preference_command_id)=36 AND substr(navigation_preference_command_id,1,4)='npc_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  workspace_membership_id TEXT NOT NULL,
  idempotency_key_hmac TEXT NOT NULL UNIQUE CHECK(length(idempotency_key_hmac)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  revision_before INTEGER NOT NULL CHECK(revision_before>=0),
  revision_after INTEGER NOT NULL CHECK(revision_after>=1),
  response_json TEXT NOT NULL CHECK(json_valid(response_json) AND json_type(response_json)='object'
    AND json(response_json)=response_json AND length(response_json)<=4096),
  outcome TEXT NOT NULL CHECK(outcome IN('created','changed','unchanged')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(workspace_membership_id) REFERENCES workspace_memberships(workspace_membership_id)
);
CREATE TABLE workspace_navigation_preference_source_mappings(
  navigation_preference_source_mapping_id TEXT PRIMARY KEY CHECK(
    length(navigation_preference_source_mapping_id)=36
    AND substr(navigation_preference_source_mapping_id,1,4)='nsm_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  workspace_membership_id TEXT NOT NULL,
  source_preference_id TEXT NOT NULL,
  source_navigation_id TEXT,
  mapped_route_code TEXT,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  mapping_code TEXT NOT NULL CHECK(mapping_code IN(
    'preserved','fallback','retired','detail_parent','advanced_parent','account_parent')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(source_preference_id,workspace_membership_id),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(workspace_membership_id) REFERENCES workspace_memberships(workspace_membership_id)
);

CREATE TABLE workspace_advanced_tool_entitlement_versions(
  advanced_tool_entitlement_id TEXT PRIMARY KEY CHECK(
    length(advanced_tool_entitlement_id)=36 AND substr(advanced_tool_entitlement_id,1,4)='ate_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  tool_code TEXT NOT NULL CHECK(tool_code IN(
    'provider_error_overrides','response_cache','policy_simulator','intent_security',
    'webhook_dead_letters','debug_console','deployment_summary')),
  route_code TEXT NOT NULL CHECK(length(route_code) BETWEEN 3 AND 120),
  required_permissions_json TEXT NOT NULL CHECK(json_valid(required_permissions_json)),
  availability TEXT NOT NULL CHECK(availability IN('available','blocked','revoked','not_applicable')),
  reason_code TEXT,
  source_revision INTEGER NOT NULL CHECK(source_revision>=0),
  generation INTEGER NOT NULL CHECK(generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  entitlement_sha256 TEXT NOT NULL CHECK(length(entitlement_sha256)=64),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(workspace_id,tenant_id,tool_code,generation),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE TABLE workspace_advanced_tool_entitlement_heads(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  tool_code TEXT NOT NULL,
  advanced_tool_entitlement_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL CHECK(generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,tenant_id,tool_code),
  FOREIGN KEY(advanced_tool_entitlement_id)
    REFERENCES workspace_advanced_tool_entitlement_versions(advanced_tool_entitlement_id),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE TABLE workspace_advanced_tool_entitlement_sources(
  advanced_tool_entitlement_id TEXT NOT NULL,
  source_code TEXT NOT NULL CHECK(source_code IN('catalog','product','deployment')),
  source_revision INTEGER NOT NULL CHECK(source_revision>=0),
  source_watermark TEXT NOT NULL CHECK(length(source_watermark) BETWEEN 1 AND 160),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  PRIMARY KEY(advanced_tool_entitlement_id,source_code),
  FOREIGN KEY(advanced_tool_entitlement_id)
    REFERENCES workspace_advanced_tool_entitlement_versions(advanced_tool_entitlement_id)
);
CREATE TRIGGER trg_workspace_advanced_tool_update_prd146
BEFORE UPDATE ON workspace_advanced_tool_entitlement_versions
BEGIN SELECT RAISE(ABORT,'workspace_advanced_tool_immutable'); END;
CREATE TRIGGER trg_workspace_advanced_tool_delete_prd146
BEFORE DELETE ON workspace_advanced_tool_entitlement_versions
BEGIN SELECT RAISE(ABORT,'workspace_advanced_tool_immutable'); END;

CREATE TABLE workspace_navigation_member_states(
  workspace_membership_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  membership_revision INTEGER NOT NULL CHECK(membership_revision>=1),
  permission_fingerprint TEXT NOT NULL CHECK(length(permission_fingerprint)=64),
  contract_snapshot_id TEXT NOT NULL,
  item_set_digest TEXT NOT NULL CHECK(length(item_set_digest)=64),
  navigation_epoch INTEGER NOT NULL CHECK(navigation_epoch>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(contract_snapshot_id) REFERENCES workspace_navigation_contract_snapshots(navigation_contract_snapshot_id)
);

CREATE TABLE workspace_navigation_events(
  workspace_navigation_event_id TEXT PRIMARY KEY CHECK(
    length(workspace_navigation_event_id)=36 AND substr(workspace_navigation_event_id,1,4)='wne_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'workspace_navigation.changed','role_task.completed','advanced_tool.opened')),
  aggregate_type TEXT NOT NULL CHECK(aggregate_type IN('navigation','task','advanced_tool')),
  aggregate_id TEXT NOT NULL CHECK(length(aggregate_id) BETWEEN 3 AND 160),
  generation INTEGER NOT NULL CHECK(generation>=1),
  event_key_sha256 TEXT NOT NULL UNIQUE CHECK(length(event_key_sha256)=64),
  idempotency_key_hmac TEXT UNIQUE CHECK(idempotency_key_hmac IS NULL OR length(idempotency_key_hmac)=64),
  request_sha256 TEXT CHECK(request_sha256 IS NULL OR length(request_sha256)=64),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'
    AND json(payload_json)=payload_json AND length(payload_json)<=2048),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  CHECK((event_type='workspace_navigation.changed' AND aggregate_type='navigation')
    OR(event_type='role_task.completed' AND aggregate_type='task')
    OR(event_type='advanced_tool.opened' AND aggregate_type='advanced_tool'))
);
CREATE TABLE workspace_navigation_event_outbox(
  workspace_navigation_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(workspace_navigation_event_outbox_id)=36
    AND substr(workspace_navigation_event_outbox_id,1,4)='wno_'),
  workspace_navigation_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','processing','dispatched','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_generation INTEGER NOT NULL CHECK(lease_generation>=0),
  lease_token_hmac TEXT CHECK(lease_token_hmac IS NULL OR length(lease_token_hmac)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  dispatched_at TEXT CHECK(dispatched_at IS NULL OR unixepoch(dispatched_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_navigation_event_id)
    REFERENCES workspace_navigation_events(workspace_navigation_event_id),
  CHECK((status='processing' AND lease_owner_id IS NOT NULL
      AND lease_token_hmac IS NOT NULL AND lease_expires_at IS NOT NULL)
    OR(status!='processing' AND lease_owner_id IS NULL
      AND lease_token_hmac IS NULL AND lease_expires_at IS NULL))
);
CREATE INDEX idx_workspace_navigation_outbox_prd146
  ON workspace_navigation_event_outbox(status,next_attempt_at,created_at);

CREATE TABLE workspace_navigation_read_audits(
  workspace_navigation_read_audit_id TEXT PRIMARY KEY CHECK(
    length(workspace_navigation_read_audit_id)=36
    AND substr(workspace_navigation_read_audit_id,1,4)='wna_'),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('workspace','identity')),
  actor_pseudonym TEXT NOT NULL CHECK(length(actor_pseudonym)=64),
  owner_digest TEXT NOT NULL CHECK(length(owner_digest)=64),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'workspace.navigation.read','workspace.task_home.read','workspace.advanced_tool.read',
    'workspace.advanced_tool.open','identity.navigation_preference.change')),
  permission_revision INTEGER NOT NULL CHECK(permission_revision>=1),
  permission_fingerprint TEXT NOT NULL CHECK(length(permission_fingerprint)=64),
  contract_generation INTEGER NOT NULL CHECK(contract_generation>=1),
  outcome TEXT NOT NULL CHECK(outcome IN(
    'success','partial','stale','empty','permission_denied','unavailable','conflict')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
CREATE INDEX idx_workspace_navigation_audit_prd146
  ON workspace_navigation_read_audits(action_code,created_at DESC,workspace_navigation_read_audit_id);

CREATE TABLE workspace_navigation_scheduler_state(
  queue_code TEXT PRIMARY KEY CHECK(queue_code IN(
    'workspace_navigation_projection','role_task_home','advanced_tool_entitlement',
    'workspace_navigation_outbox','navigation_reconciliation')),
  enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),
  fence_generation INTEGER NOT NULL CHECK(fence_generation>=1),
  lease_owner_id TEXT,
  lease_token_hmac TEXT CHECK(lease_token_hmac IS NULL OR length(lease_token_hmac)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  cursor_value TEXT,
  last_error_code TEXT,
  source_watermark TEXT NOT NULL CHECK(length(source_watermark) BETWEEN 1 AND 160),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK((lease_owner_id IS NULL AND lease_token_hmac IS NULL AND lease_expires_at IS NULL)
    OR(lease_owner_id IS NOT NULL AND lease_token_hmac IS NOT NULL
      AND lease_expires_at IS NOT NULL))
);
INSERT INTO workspace_navigation_scheduler_state
  (queue_code,enabled,fence_generation,lease_owner_id,lease_token_hmac,lease_expires_at,
   attempt_count,next_attempt_at,cursor_value,last_error_code,source_watermark,updated_at)
SELECT queue_code,1,1,NULL,NULL,NULL,0,strftime('%Y-%m-%dT%H:%M:%fZ','now'),
       NULL,NULL,'none',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM (
  SELECT 'workspace_navigation_projection' queue_code UNION ALL
  SELECT 'role_task_home' UNION ALL SELECT 'advanced_tool_entitlement' UNION ALL
  SELECT 'workspace_navigation_outbox' UNION ALL SELECT 'navigation_reconciliation');

CREATE TABLE workspace_navigation_migration_cursors(
  source_kind TEXT PRIMARY KEY CHECK(source_kind IN(
    'catalog','preference','task_home','advanced_tool','cutover')),
  source_row_count INTEGER NOT NULL CHECK(source_row_count>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  target_row_count INTEGER NOT NULL CHECK(target_row_count>=0),
  owner_coverage_count INTEGER NOT NULL CHECK(owner_coverage_count>=0),
  source_watermark TEXT NOT NULL CHECK(length(source_watermark) BETWEEN 1 AND 160),
  state TEXT NOT NULL CHECK(state IN('inventory','backfilled','blocked','cutover')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO workspace_navigation_migration_cursors VALUES
  ('catalog',63,metahub_sha256('navigation-source:63'),0,0,'0137','inventory',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('preference',(SELECT COUNT(*) FROM user_preferences),metahub_sha256('preference:'||(SELECT COUNT(*) FROM user_preferences)),0,0,
    coalesce((SELECT max(updated_at) FROM user_preferences),'none'),'inventory',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('task_home',0,metahub_sha256('task-home:0'),0,0,'none','inventory',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('advanced_tool',7,metahub_sha256('advanced-tool:7'),0,0,'catalog-v1','inventory',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('cutover',0,metahub_sha256('cutover:0'),0,0,'none','inventory',1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TABLE workspace_navigation_reconciliations(
  workspace_navigation_reconciliation_id TEXT PRIMARY KEY CHECK(
    length(workspace_navigation_reconciliation_id)=36
    AND substr(workspace_navigation_reconciliation_id,1,4)='wnr_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  check_code TEXT NOT NULL CHECK(check_code IN(
    'catalog','owner','preference','task_home','advanced_tool','event_outbox','watermark')),
  source_count INTEGER NOT NULL CHECK(source_count>=0),
  target_count INTEGER NOT NULL CHECK(target_count>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  target_sha256 TEXT NOT NULL CHECK(length(target_sha256)=64),
  outcome TEXT NOT NULL CHECK(outcome IN('matched','mismatch','blocked')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);

CREATE TRIGGER trg_workspace_navigation_preference_owner_prd146
BEFORE UPDATE ON workspace_navigation_preferences FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id OR NEW.workspace_membership_id!=OLD.workspace_membership_id
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'workspace_navigation_preference_owner_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_event_update_prd146
BEFORE UPDATE ON workspace_navigation_events
BEGIN SELECT RAISE(ABORT,'workspace_navigation_event_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_event_delete_prd146
BEFORE DELETE ON workspace_navigation_events
BEGIN SELECT RAISE(ABORT,'workspace_navigation_event_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_audit_update_prd146
BEFORE UPDATE ON workspace_navigation_read_audits
BEGIN SELECT RAISE(ABORT,'workspace_navigation_audit_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_audit_delete_prd146
BEFORE DELETE ON workspace_navigation_read_audits
BEGIN SELECT RAISE(ABORT,'workspace_navigation_audit_immutable'); END;
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

COMMIT;
PRAGMA foreign_keys = ON;

CREATE TEMP TABLE prd146_post_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd146_post_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok')
  + CASE WHEN (SELECT COUNT(*) FROM workspace_navigation_permission_extensions)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_navigation_role_permissions)=26 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_navigation_scheduler_state)=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_navigation_migration_cursors)=5 THEN 0 ELSE 1 END;
DROP TABLE prd146_post_guard;
