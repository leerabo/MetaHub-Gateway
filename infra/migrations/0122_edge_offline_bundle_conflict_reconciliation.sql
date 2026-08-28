-- 建立隔离环境签名包、断点续传水位和显式冲突解决事实。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd130_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd130_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0121' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'edge_deployments','workspace_memberships','platform_operators',
      'tenant_deletion_table_manifest','tenant_deletion_trigger_manifest'))=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd130')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd130_up_guard;

CREATE TABLE workspace_edge_offline_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'edge.offline_bundle.read','edge.offline_bundle.transfer',
    'edge.offline_conflict.resolve')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-130'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=40)
);
CREATE TRIGGER trg_workspace_edge_offline_permissions_update_prd130
BEFORE UPDATE ON workspace_edge_offline_permission_extensions
BEGIN SELECT RAISE(ABORT,'edge_offline_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_offline_permissions_delete_prd130
BEFORE DELETE ON workspace_edge_offline_permission_extensions
BEGIN SELECT RAISE(ABORT,'edge_offline_permission_immutable'); END;
CREATE TABLE workspace_edge_offline_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_edge_offline_permission_extensions(permission_code)
);
CREATE TRIGGER trg_workspace_edge_offline_roles_insert_prd130
BEFORE INSERT ON workspace_edge_offline_role_permissions FOR EACH ROW
WHEN NOT(
  NEW.role_code='owner'
  OR(NEW.role_code='admin' AND NEW.permission_code IN(
    'edge.offline_bundle.read','edge.offline_bundle.transfer'))
  OR(NEW.role_code='security_admin')
  OR(NEW.role_code='auditor' AND NEW.permission_code='edge.offline_bundle.read'))
BEGIN SELECT RAISE(ABORT,'edge_offline_role_permission_invalid'); END;
CREATE TRIGGER trg_workspace_edge_offline_roles_update_prd130
BEFORE UPDATE ON workspace_edge_offline_role_permissions
BEGIN SELECT RAISE(ABORT,'edge_offline_role_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_offline_roles_delete_prd130
BEFORE DELETE ON workspace_edge_offline_role_permissions
BEGIN SELECT RAISE(ABORT,'edge_offline_role_permission_immutable'); END;
INSERT INTO workspace_edge_offline_permission_extensions VALUES
  ('edge.offline_bundle.read','PRD-130',40),
  ('edge.offline_bundle.transfer','PRD-130',40),
  ('edge.offline_conflict.resolve','PRD-130',40);
INSERT INTO workspace_edge_offline_role_permissions VALUES
  ('owner','edge.offline_bundle.read'),('owner','edge.offline_bundle.transfer'),
  ('owner','edge.offline_conflict.resolve'),('admin','edge.offline_bundle.read'),
  ('admin','edge.offline_bundle.transfer'),('security_admin','edge.offline_bundle.read'),
  ('security_admin','edge.offline_bundle.transfer'),
  ('security_admin','edge.offline_conflict.resolve'),
  ('auditor','edge.offline_bundle.read');

CREATE TABLE platform_edge_offline_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.edge_offline_bundle.read','platform.edge_offline_bundle.manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-130'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=40)
);
CREATE TRIGGER trg_platform_edge_offline_permissions_update_prd130
BEFORE UPDATE ON platform_edge_offline_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_offline_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_offline_permissions_delete_prd130
BEFORE DELETE ON platform_edge_offline_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_offline_permission_immutable'); END;
CREATE TABLE platform_edge_offline_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'platform_owner','platform_ops','platform_security','platform_billing',
    'platform_verification','platform_compliance','platform_privacy',
    'platform_support','release_manager','platform_provider_engineer','platform_channel',
    'platform_customer_success','platform_growth')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_edge_offline_permission_extensions(permission_code)
);
CREATE TRIGGER trg_platform_edge_offline_roles_insert_prd130
BEFORE INSERT ON platform_edge_offline_role_permissions FOR EACH ROW
WHEN NOT(
  NEW.role_code IN('platform_owner','platform_ops')
  OR(NEW.role_code IN('platform_security','release_manager')
    AND NEW.permission_code='platform.edge_offline_bundle.manage')
  OR(NEW.role_code IN('platform_security','release_manager')
    AND NEW.permission_code='platform.edge_offline_bundle.read'))
BEGIN SELECT RAISE(ABORT,'platform_edge_offline_role_permission_invalid'); END;
CREATE TRIGGER trg_platform_edge_offline_roles_update_prd130
BEFORE UPDATE ON platform_edge_offline_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_offline_role_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_offline_roles_delete_prd130
BEFORE DELETE ON platform_edge_offline_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_offline_role_permission_immutable'); END;
INSERT INTO platform_edge_offline_permission_extensions VALUES
  ('platform.edge_offline_bundle.read','PRD-130',40),
  ('platform.edge_offline_bundle.manage','PRD-130',40);
INSERT INTO platform_edge_offline_role_permissions VALUES
  ('platform_owner','platform.edge_offline_bundle.read'),
  ('platform_owner','platform.edge_offline_bundle.manage'),
  ('platform_ops','platform.edge_offline_bundle.read'),
  ('platform_ops','platform.edge_offline_bundle.manage'),
  ('platform_security','platform.edge_offline_bundle.read'),
  ('platform_security','platform.edge_offline_bundle.manage'),
  ('release_manager','platform.edge_offline_bundle.read'),
  ('release_manager','platform.edge_offline_bundle.manage');

-- Workspace 请求和 Platform 批准分离；签名 manifest 只在批准后成为不可变 bundle。
CREATE TABLE edge_offline_bundle_requests(
  offline_bundle_request_id TEXT PRIMARY KEY CHECK(length(offline_bundle_request_id)=36
    AND substr(offline_bundle_request_id,1,4)='obr_'
    AND substr(offline_bundle_request_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN('export','import','resolve')),
  status TEXT NOT NULL CHECK(status IN('pending_platform','approved','rejected','expired')),
  requested_by_account_id TEXT NOT NULL,
  approved_by_operator_id TEXT,
  target_offline_bundle_id TEXT,
  target_offline_conflict_id TEXT,
  proposal_json TEXT NOT NULL CHECK(json_valid(proposal_json)),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  revision INTEGER NOT NULL CHECK(revision>=1),
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  decided_at TEXT CHECK(decided_at IS NULL OR unixepoch(decided_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(requested_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(approved_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status='pending_platform' AND approved_by_operator_id IS NULL AND decided_at IS NULL)
    OR(status!='pending_platform' AND decided_at IS NOT NULL)),
  CHECK((operation='resolve' AND target_offline_conflict_id IS NOT NULL)
    OR(operation!='resolve' AND target_offline_conflict_id IS NULL))
);
CREATE INDEX idx_edge_offline_requests_owner_prd130
  ON edge_offline_bundle_requests(workspace_id,tenant_id,edge_deployment_id,updated_at DESC);
CREATE TRIGGER trg_edge_offline_requests_update_prd130
BEFORE UPDATE ON edge_offline_bundle_requests FOR EACH ROW
WHEN NEW.offline_bundle_request_id!=OLD.offline_bundle_request_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.operation!=OLD.operation
  OR NEW.requested_by_account_id!=OLD.requested_by_account_id
  OR NEW.proposal_json!=OLD.proposal_json
  OR NEW.reason_code!=OLD.reason_code OR NEW.request_sha256!=OLD.request_sha256
  OR NEW.requested_at!=OLD.requested_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at OR OLD.status!='pending_platform'
BEGIN SELECT RAISE(ABORT,'edge_offline_request_transition_invalid'); END;
CREATE TRIGGER trg_edge_offline_requests_delete_prd130
BEFORE DELETE ON edge_offline_bundle_requests
BEGIN SELECT RAISE(ABORT,'edge_offline_request_immutable'); END;

CREATE TABLE edge_offline_bundles(
  offline_bundle_id TEXT PRIMARY KEY CHECK(length(offline_bundle_id)=36
    AND substr(offline_bundle_id,1,4)='obb_'
    AND substr(offline_bundle_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN('cloud_to_edge','edge_to_cloud')),
  producer_generation INTEGER NOT NULL CHECK(producer_generation>=1),
  domain_count INTEGER NOT NULL CHECK(domain_count BETWEEN 1 AND 7),
  domain_codes_json TEXT NOT NULL CHECK(json_valid(domain_codes_json)
    AND json_type(domain_codes_json)='array'),
  entry_count INTEGER NOT NULL CHECK(entry_count BETWEEN 1 AND 1024),
  encrypted_size_bytes INTEGER NOT NULL CHECK(encrypted_size_bytes BETWEEN 17 AND 10737418240),
  version_vector_sha256 TEXT NOT NULL CHECK(length(version_vector_sha256)=64
    AND version_vector_sha256 NOT GLOB '*[^0-9a-f]*'),
  entry_merkle_root TEXT NOT NULL CHECK(length(entry_merkle_root)=64
    AND entry_merkle_root NOT GLOB '*[^0-9a-f]*'),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  signature_key_id TEXT NOT NULL CHECK(length(signature_key_id) BETWEEN 3 AND 80
    AND signature_key_id=lower(signature_key_id)
    AND signature_key_id NOT GLOB '*[^a-z0-9._:-]*'),
  encryption_key_version INTEGER NOT NULL CHECK(encryption_key_version>=1),
  kms_key_id_sha256 TEXT NOT NULL CHECK(length(kms_key_id_sha256)=64
    AND kms_key_id_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  UNIQUE(workspace_id,tenant_id,offline_bundle_id),
  UNIQUE(edge_deployment_id,direction,producer_generation,manifest_sha256),
  CHECK(unixepoch(expires_at)>unixepoch(created_at))
);
CREATE INDEX idx_edge_offline_bundles_owner_prd130
  ON edge_offline_bundles(workspace_id,tenant_id,edge_deployment_id,created_at DESC);
CREATE TRIGGER trg_edge_offline_bundles_update_prd130
BEFORE UPDATE ON edge_offline_bundles
BEGIN SELECT RAISE(ABORT,'edge_offline_bundle_immutable'); END;
CREATE TRIGGER trg_edge_offline_bundles_delete_prd130
BEFORE DELETE ON edge_offline_bundles
BEGIN SELECT RAISE(ABORT,'edge_offline_bundle_immutable'); END;

CREATE TABLE edge_offline_bundle_transfers(
  offline_bundle_transfer_id TEXT PRIMARY KEY CHECK(length(offline_bundle_transfer_id)=36
    AND substr(offline_bundle_transfer_id,1,4)='obt_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  offline_bundle_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN(
    'prepared','transferring','verified','applied','conflict','failed')),
  chunk_count INTEGER NOT NULL CHECK(chunk_count BETWEEN 1 AND 1048576),
  next_sequence INTEGER NOT NULL CHECK(next_sequence BETWEEN 0 AND chunk_count),
  bytes_verified INTEGER NOT NULL CHECK(bytes_verified BETWEEN 0 AND 10737418240),
  media_receipt_sha256 TEXT CHECK(media_receipt_sha256 IS NULL OR(
    length(media_receipt_sha256)=64 AND media_receipt_sha256 NOT GLOB '*[^0-9a-f]*')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  started_at TEXT NOT NULL CHECK(unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,offline_bundle_id)
    REFERENCES edge_offline_bundles(workspace_id,tenant_id,offline_bundle_id),
  UNIQUE(workspace_id,tenant_id,offline_bundle_transfer_id),
  UNIQUE(offline_bundle_id),
  CHECK((status IN('applied','conflict','failed') AND completed_at IS NOT NULL)
    OR(status NOT IN('applied','conflict','failed') AND completed_at IS NULL)),
  CHECK((status IN('verified','applied','conflict') AND next_sequence=chunk_count
      AND media_receipt_sha256 IS NOT NULL)
    OR status NOT IN('verified','applied','conflict'))
);
CREATE INDEX idx_edge_offline_transfers_owner_prd130
  ON edge_offline_bundle_transfers(workspace_id,tenant_id,edge_deployment_id,updated_at DESC);
CREATE TRIGGER trg_edge_offline_transfers_update_prd130
BEFORE UPDATE ON edge_offline_bundle_transfers FOR EACH ROW
WHEN NEW.offline_bundle_transfer_id!=OLD.offline_bundle_transfer_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.offline_bundle_id!=OLD.offline_bundle_id
  OR NEW.chunk_count!=OLD.chunk_count OR NEW.next_sequence<OLD.next_sequence
  OR NEW.bytes_verified<OLD.bytes_verified OR NEW.revision!=OLD.revision+1
  OR NEW.started_at!=OLD.started_at OR NEW.updated_at<=OLD.updated_at
  OR OLD.status IN('applied','conflict','failed')
BEGIN SELECT RAISE(ABORT,'edge_offline_transfer_transition_invalid'); END;
CREATE TRIGGER trg_edge_offline_transfers_delete_prd130
BEFORE DELETE ON edge_offline_bundle_transfers
BEGIN SELECT RAISE(ABORT,'edge_offline_transfer_immutable'); END;

CREATE TABLE edge_offline_sync_cursors(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN('cloud_to_edge','edge_to_cloud')),
  domain TEXT NOT NULL CHECK(domain IN(
    'desired_state','license','release','status','usage','audit','validation')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  value_sha256 TEXT NOT NULL CHECK(length(value_sha256)=64
    AND value_sha256 NOT GLOB '*[^0-9a-f]*'),
  offline_bundle_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,tenant_id,edge_deployment_id,direction,domain),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,offline_bundle_id)
    REFERENCES edge_offline_bundles(workspace_id,tenant_id,offline_bundle_id)
);
CREATE TRIGGER trg_edge_offline_cursors_update_prd130
BEFORE UPDATE ON edge_offline_sync_cursors FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.direction!=OLD.direction
  OR NEW.domain!=OLD.domain OR NEW.generation<OLD.generation
  OR(NEW.generation=OLD.generation AND NEW.value_sha256!=OLD.value_sha256)
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'edge_offline_cursor_transition_invalid'); END;
CREATE TRIGGER trg_edge_offline_cursors_delete_prd130
BEFORE DELETE ON edge_offline_sync_cursors
BEGIN SELECT RAISE(ABORT,'edge_offline_cursor_immutable'); END;

CREATE TABLE edge_offline_conflicts(
  offline_conflict_id TEXT PRIMARY KEY CHECK(length(offline_conflict_id)=36
    AND substr(offline_conflict_id,1,4)='ofc_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  offline_bundle_id TEXT NOT NULL,
  domain TEXT NOT NULL CHECK(domain IN(
    'desired_state','license','release','status','usage','audit','validation')),
  conflict_type TEXT NOT NULL CHECK(conflict_type='divergent_update'),
  base_generation INTEGER NOT NULL CHECK(base_generation>=0),
  base_sha256 TEXT CHECK(base_sha256 IS NULL OR(length(base_sha256)=64
    AND base_sha256 NOT GLOB '*[^0-9a-f]*')),
  cloud_generation INTEGER NOT NULL CHECK(cloud_generation>=1),
  cloud_sha256 TEXT NOT NULL CHECK(length(cloud_sha256)=64
    AND cloud_sha256 NOT GLOB '*[^0-9a-f]*'),
  local_generation INTEGER NOT NULL CHECK(local_generation>=1),
  local_sha256 TEXT NOT NULL CHECK(length(local_sha256)=64
    AND local_sha256 NOT GLOB '*[^0-9a-f]*'),
  detected_at TEXT NOT NULL CHECK(unixepoch(detected_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,offline_bundle_id)
    REFERENCES edge_offline_bundles(workspace_id,tenant_id,offline_bundle_id),
  UNIQUE(workspace_id,tenant_id,offline_conflict_id),
  UNIQUE(offline_bundle_id,domain),
  CHECK((base_generation=0 AND base_sha256 IS NULL)
    OR(base_generation>0 AND base_sha256 IS NOT NULL))
);
CREATE INDEX idx_edge_offline_conflicts_owner_prd130
  ON edge_offline_conflicts(workspace_id,tenant_id,edge_deployment_id,detected_at DESC);
CREATE TRIGGER trg_edge_offline_conflicts_update_prd130
BEFORE UPDATE ON edge_offline_conflicts
BEGIN SELECT RAISE(ABORT,'edge_offline_conflict_immutable'); END;
CREATE TRIGGER trg_edge_offline_conflicts_delete_prd130
BEFORE DELETE ON edge_offline_conflicts
BEGIN SELECT RAISE(ABORT,'edge_offline_conflict_immutable'); END;

CREATE TABLE edge_offline_conflict_resolutions(
  offline_conflict_resolution_id TEXT PRIMARY KEY CHECK(
    length(offline_conflict_resolution_id)=36
    AND substr(offline_conflict_resolution_id,1,4)='ofr_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  offline_conflict_id TEXT NOT NULL,
  resolution TEXT NOT NULL CHECK(resolution IN(
    'keep_cloud','keep_local','supersede_with_new')),
  resolved_by_account_id TEXT NOT NULL,
  approved_by_operator_id TEXT NOT NULL,
  new_generation INTEGER NOT NULL CHECK(new_generation>=1),
  new_value_sha256 TEXT NOT NULL CHECK(length(new_value_sha256)=64
    AND new_value_sha256 NOT GLOB '*[^0-9a-f]*'),
  approval_sha256 TEXT NOT NULL CHECK(length(approval_sha256)=64
    AND approval_sha256 NOT GLOB '*[^0-9a-f]*'),
  resolved_at TEXT NOT NULL CHECK(unixepoch(resolved_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,offline_conflict_id)
    REFERENCES edge_offline_conflicts(workspace_id,tenant_id,offline_conflict_id),
  FOREIGN KEY(resolved_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(approved_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(workspace_id,tenant_id,offline_conflict_id)
);
CREATE TRIGGER trg_edge_offline_resolutions_update_prd130
BEFORE UPDATE ON edge_offline_conflict_resolutions
BEGIN SELECT RAISE(ABORT,'edge_offline_resolution_immutable'); END;
CREATE TRIGGER trg_edge_offline_resolutions_delete_prd130
BEFORE DELETE ON edge_offline_conflict_resolutions
BEGIN SELECT RAISE(ABORT,'edge_offline_resolution_immutable'); END;

CREATE TABLE edge_offline_bundle_events(
  edge_offline_bundle_event_id TEXT PRIMARY KEY CHECK(length(edge_offline_bundle_event_id)=36
    AND substr(edge_offline_bundle_event_id,1,4)='oev_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'edge.offline_bundle_exported','edge.offline_bundle_applied',
    'edge.offline_conflict_detected','edge.offline_conflict_resolved')),
  aggregate_type TEXT NOT NULL CHECK(aggregate_type IN('bundle','conflict')),
  aggregate_id TEXT NOT NULL CHECK(length(aggregate_id)=36),
  safe_payload_json TEXT NOT NULL CHECK(json_valid(safe_payload_json)),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 128),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  UNIQUE(workspace_id,tenant_id,edge_offline_bundle_event_id)
);
CREATE TRIGGER trg_edge_offline_events_update_prd130
BEFORE UPDATE ON edge_offline_bundle_events
BEGIN SELECT RAISE(ABORT,'edge_offline_event_immutable'); END;
CREATE TRIGGER trg_edge_offline_events_delete_prd130
BEFORE DELETE ON edge_offline_bundle_events
BEGIN SELECT RAISE(ABORT,'edge_offline_event_immutable'); END;

CREATE TABLE edge_offline_bundle_outbox(
  edge_offline_bundle_outbox_id TEXT PRIMARY KEY CHECK(length(edge_offline_bundle_outbox_id)=36
    AND substr(edge_offline_bundle_outbox_id,1,4)='oob_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_offline_bundle_event_id TEXT NOT NULL,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('audit','fleet')),
  status TEXT NOT NULL CHECK(status IN('pending','delivered')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 1000),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_offline_bundle_event_id)
    REFERENCES edge_offline_bundle_events(workspace_id,tenant_id,edge_offline_bundle_event_id),
  UNIQUE(edge_offline_bundle_event_id,delivery_domain)
);
CREATE TRIGGER trg_edge_offline_outbox_update_prd130
BEFORE UPDATE ON edge_offline_bundle_outbox FOR EACH ROW
WHEN NEW.edge_offline_bundle_outbox_id!=OLD.edge_offline_bundle_outbox_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_offline_bundle_event_id!=OLD.edge_offline_bundle_event_id
  OR NEW.delivery_domain!=OLD.delivery_domain OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count!=OLD.attempt_count+1 OR NEW.updated_at<=OLD.updated_at
  OR OLD.status='delivered'
BEGIN SELECT RAISE(ABORT,'edge_offline_outbox_transition_invalid'); END;
CREATE TRIGGER trg_edge_offline_outbox_delete_prd130
BEFORE DELETE ON edge_offline_bundle_outbox
BEGIN SELECT RAISE(ABORT,'edge_offline_outbox_immutable'); END;

CREATE TABLE edge_offline_bundle_action_commands(
  edge_offline_bundle_action_command_id TEXT PRIMARY KEY CHECK(
    length(edge_offline_bundle_action_command_id)=36
    AND substr(edge_offline_bundle_action_command_id,1,4)='oac_'),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('workspace','platform')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 2 AND 160),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'edge.offline.export.request','edge.offline.import.request',
    'edge.offline.resolve.request','edge.offline.request.approve')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(actor_domain,actor_id,action_code,idempotency_sha256)
);
CREATE TRIGGER trg_edge_offline_actions_update_prd130
BEFORE UPDATE ON edge_offline_bundle_action_commands
BEGIN SELECT RAISE(ABORT,'edge_offline_action_immutable'); END;
CREATE TRIGGER trg_edge_offline_actions_delete_prd130
BEFORE DELETE ON edge_offline_bundle_action_commands
BEGIN SELECT RAISE(ABORT,'edge_offline_action_immutable'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd130(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-130'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd130_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd130_manifest_sources VALUES
  ('edge_offline_bundle_requests','archive_then_delete','audit'),
  ('edge_offline_bundles','archive_then_delete','audit'),
  ('edge_offline_bundle_transfers','archive_then_delete','audit'),
  ('edge_offline_sync_cursors','delete','operational'),
  ('edge_offline_conflicts','archive_then_delete','audit'),
  ('edge_offline_conflict_resolutions','archive_then_delete','audit'),
  ('edge_offline_bundle_events','archive_then_delete','audit'),
  ('edge_offline_bundle_outbox','delete','operational');
INSERT INTO tenant_deletion_table_manifest(
  source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
  trigger_set_sha256,catalog_revision,created_at)
SELECT table_record.name,source.disposition,source.retention_class,
  metahub_sha256(table_record.sql),
  metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info(table_record.name) ORDER BY cid))),
  metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT trigger_record.name||char(31)||metahub_sha256(trigger_record.sql) AS record
    FROM sqlite_schema trigger_record WHERE trigger_record.type='trigger'
      AND trigger_record.tbl_name=table_record.name ORDER BY trigger_record.name)),'')),
  1,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM sqlite_schema table_record
JOIN prd130_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd130(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-130',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd130_manifest_sources ORDER BY source_table;
DROP TABLE prd130_manifest_sources;
DELETE FROM tenant_deletion_trigger_manifest;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT trigger_record.name,trigger_record.tbl_name,trigger_record.sql,
  metahub_sha256(trigger_record.sql)
FROM sqlite_schema trigger_record
JOIN tenant_deletion_table_manifest manifest ON manifest.source_table=trigger_record.tbl_name
WHERE trigger_record.type='trigger' ORDER BY trigger_record.tbl_name,trigger_record.name;
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd130_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd130
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd130_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd130
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd130_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd130
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd130_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd130_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_edge_offline_permission_extensions)=3
      AND (SELECT COUNT(*) FROM workspace_edge_offline_role_permissions)=9
      AND (SELECT COUNT(*) FROM platform_edge_offline_permission_extensions)=2
      AND (SELECT COUNT(*) FROM platform_edge_offline_role_permissions)=8
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd130)=8
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd130_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
