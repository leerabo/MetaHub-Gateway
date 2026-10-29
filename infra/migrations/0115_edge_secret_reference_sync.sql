-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd123_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd123_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0114' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'edge_deployments','edge_device_identities','edge_device_certificates','edge_sessions',
      'workspace_edge_permission_extensions','workspace_edge_telemetry_permission_extensions',
      'tenant_deletion_table_manifest','tenant_deletion_trigger_manifest'))=8
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd123')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd123_up_guard;

CREATE TABLE workspace_edge_secret_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code='edge.secret_reference.read'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-123'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=33)
);
CREATE TABLE workspace_edge_secret_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code) REFERENCES workspace_edge_secret_permission_extensions(permission_code)
);
INSERT INTO workspace_edge_secret_permission_extensions VALUES
  ('edge.secret_reference.read','PRD-123',33);
INSERT INTO workspace_edge_secret_role_permissions VALUES
  ('owner','edge.secret_reference.read'),
  ('admin','edge.secret_reference.read'),
  ('security_admin','edge.secret_reference.read'),
  ('auditor','edge.secret_reference.read');
CREATE TRIGGER trg_workspace_edge_secret_permissions_insert_prd123
BEFORE INSERT ON workspace_edge_secret_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_secret_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_secret_permissions_update_prd123
BEFORE UPDATE ON workspace_edge_secret_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_secret_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_secret_permissions_delete_prd123
BEFORE DELETE ON workspace_edge_secret_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_secret_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_secret_roles_insert_prd123
BEFORE INSERT ON workspace_edge_secret_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_secret_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_secret_roles_update_prd123
BEFORE UPDATE ON workspace_edge_secret_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_secret_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_secret_roles_delete_prd123
BEFORE DELETE ON workspace_edge_secret_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_secret_permission_immutable'); END;

CREATE TABLE edge_secret_references(
  reference_id TEXT PRIMARY KEY CHECK(length(reference_id)=36
    AND substr(reference_id,1,4)='lsr_'
    AND substr(reference_id,5) NOT GLOB '*[^0-9a-f]*'),
  schema_version TEXT NOT NULL CHECK(schema_version='1'),
  workspace_id TEXT NOT NULL CHECK(length(workspace_id) BETWEEN 3 AND 80
    AND workspace_id GLOB '[a-z0-9]*' AND workspace_id NOT GLOB '*[^a-z0-9_:-]*'),
  tenant_id TEXT NOT NULL CHECK(length(tenant_id) BETWEEN 3 AND 80
    AND tenant_id GLOB '[a-z0-9]*' AND tenant_id NOT GLOB '*[^a-z0-9_:-]*'),
  edge_deployment_id TEXT NOT NULL,
  provider_connection_id TEXT NOT NULL CHECK(length(provider_connection_id) BETWEEN 3 AND 80
    AND provider_connection_id GLOB '[a-z0-9]*'
    AND provider_connection_id NOT GLOB '*[^a-z0-9_:-]*'),
  label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 80),
  kind TEXT NOT NULL CHECK(kind IN(
    'api_key','bearer_token','basic_password','oauth_client_secret')),
  status TEXT NOT NULL CHECK(status IN('active','rotating','degraded','disabled')),
  revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
  active_version INTEGER CHECK(active_version IS NULL
    OR active_version BETWEEN 1 AND 2147483647),
  last_test_outcome TEXT CHECK(last_test_outcome IS NULL OR last_test_outcome IN(
    'succeeded','authentication_failed','authorization_failed','rate_limited','timeout',
    'tls_failed','network_unreachable','provider_unavailable','unsupported')),
  capabilities_json TEXT NOT NULL CHECK(capabilities_json IN(
    '[]','["chat_completions"]','["embeddings"]','["models"]',
    '["chat_completions","embeddings"]','["chat_completions","models"]',
    '["embeddings","models"]','["chat_completions","embeddings","models"]')),
  rotated_at TEXT CHECK(rotated_at IS NULL OR unixepoch(rotated_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256)=64
    AND projection_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_event_id TEXT NOT NULL CHECK(length(source_event_id)=36
    AND substr(source_event_id,1,4)='lse_'
    AND substr(source_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  received_at TEXT NOT NULL CHECK(unixepoch(received_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,reference_id),
  CHECK((status='active' AND active_version IS NOT NULL)
    OR(status='disabled' AND active_version IS NULL)
    OR status IN('rotating','degraded')),
  CHECK(last_test_outcome IS NOT NULL OR capabilities_json='[]')
);
CREATE INDEX idx_edge_secret_references_owner_prd123
  ON edge_secret_references(workspace_id,tenant_id,edge_deployment_id,status,updated_at);
CREATE TRIGGER trg_edge_secret_references_update_prd123
BEFORE UPDATE ON edge_secret_references FOR EACH ROW
WHEN NEW.reference_id!=OLD.reference_id OR NEW.schema_version!=OLD.schema_version
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.provider_connection_id!=OLD.provider_connection_id OR NEW.label!=OLD.label
  OR NEW.kind!=OLD.kind OR NEW.revision<=OLD.revision OR NEW.updated_at<OLD.updated_at
  OR NEW.received_at<OLD.received_at
BEGIN SELECT RAISE(ABORT,'edge_secret_reference_transition_invalid'); END;
CREATE TRIGGER trg_edge_secret_references_delete_prd123
BEFORE DELETE ON edge_secret_references
BEGIN SELECT RAISE(ABORT,'edge_secret_reference_immutable'); END;

CREATE TABLE edge_secret_sync_batches(
  edge_secret_sync_batch_id TEXT PRIMARY KEY CHECK(length(edge_secret_sync_batch_id)=36
    AND substr(edge_secret_sync_batch_id,1,4)='lsb_'
    AND substr(edge_secret_sync_batch_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_device_identity_id TEXT NOT NULL,
  edge_device_certificate_id TEXT NOT NULL,
  edge_session_id TEXT NOT NULL,
  schema_version TEXT NOT NULL CHECK(schema_version='1'),
  event_count INTEGER NOT NULL CHECK(event_count BETWEEN 1 AND 64),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  first_event_at TEXT NOT NULL CHECK(unixepoch(first_event_at) IS NOT NULL),
  last_event_at TEXT NOT NULL CHECK(unixepoch(last_event_at) IS NOT NULL),
  sent_at TEXT NOT NULL CHECK(unixepoch(sent_at) IS NOT NULL),
  received_at TEXT NOT NULL CHECK(unixepoch(received_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_device_identity_id)
    REFERENCES edge_device_identities(
      workspace_id,tenant_id,edge_deployment_id,edge_device_identity_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_device_certificate_id)
    REFERENCES edge_device_certificates(
      workspace_id,tenant_id,edge_deployment_id,edge_device_certificate_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_session_id,
    edge_device_identity_id,edge_device_certificate_id)
    REFERENCES edge_sessions(workspace_id,tenant_id,edge_deployment_id,edge_session_id,
      edge_device_identity_id,edge_device_certificate_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_secret_sync_batch_id),
  CHECK(first_event_at<=last_event_at AND last_event_at<=sent_at)
);
CREATE INDEX idx_edge_secret_sync_batches_owner_prd123
  ON edge_secret_sync_batches(workspace_id,tenant_id,edge_deployment_id,received_at);
CREATE TRIGGER trg_edge_secret_sync_batches_update_prd123
BEFORE UPDATE ON edge_secret_sync_batches
BEGIN SELECT RAISE(ABORT,'edge_secret_sync_batch_immutable'); END;
CREATE TRIGGER trg_edge_secret_sync_batches_delete_prd123
BEFORE DELETE ON edge_secret_sync_batches
BEGIN SELECT RAISE(ABORT,'edge_secret_sync_batch_immutable'); END;

CREATE TABLE edge_secret_events(
  edge_secret_event_id TEXT PRIMARY KEY CHECK(length(edge_secret_event_id)=36
    AND substr(edge_secret_event_id,1,4)='lse_'
    AND substr(edge_secret_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_secret_sync_batch_id TEXT NOT NULL,
  reference_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'edge.secret_reference_changed','edge.secret_rotation_failed',
    'edge.provider_test_completed')),
  revision INTEGER CHECK(revision IS NULL OR revision BETWEEN 1 AND 2147483647),
  version INTEGER CHECK(version IS NULL OR version BETWEEN 1 AND 2147483647),
  outcome TEXT CHECK(outcome IS NULL OR outcome IN(
    'succeeded','authentication_failed','authorization_failed','rate_limited','timeout',
    'tls_failed','network_unreachable','provider_unavailable','unsupported')),
  capabilities_json TEXT CHECK(capabilities_json IS NULL OR capabilities_json IN(
    '[]','["chat_completions"]','["embeddings"]','["models"]',
    '["chat_completions","embeddings"]','["chat_completions","models"]',
    '["embeddings","models"]','["chat_completions","embeddings","models"]')),
  latency_bucket TEXT CHECK(latency_bucket IS NULL OR latency_bucket IN(
    'lt_100ms','100_499ms','500_1999ms','gte_2000ms')),
  event_payload_sha256 TEXT NOT NULL CHECK(length(event_payload_sha256)=64
    AND event_payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  received_at TEXT NOT NULL CHECK(unixepoch(received_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_secret_sync_batch_id)
    REFERENCES edge_secret_sync_batches(
      workspace_id,tenant_id,edge_deployment_id,edge_secret_sync_batch_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,reference_id)
    REFERENCES edge_secret_references(workspace_id,tenant_id,edge_deployment_id,reference_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_secret_event_id),
  CHECK((event_type='edge.secret_reference_changed' AND revision IS NOT NULL
      AND version IS NULL AND capabilities_json IS NOT NULL AND latency_bucket IS NULL)
    OR(event_type='edge.secret_rotation_failed' AND revision IS NOT NULL
      AND version IS NULL AND outcome IS NOT NULL AND capabilities_json IS NULL
      AND latency_bucket IS NULL)
    OR(event_type='edge.provider_test_completed' AND revision IS NULL
      AND version IS NOT NULL AND outcome IS NOT NULL AND capabilities_json IS NOT NULL
      AND latency_bucket IS NOT NULL))
);
CREATE INDEX idx_edge_secret_events_owner_prd123
  ON edge_secret_events(workspace_id,tenant_id,edge_deployment_id,reference_id,occurred_at);
CREATE TRIGGER trg_edge_secret_events_update_prd123
BEFORE UPDATE ON edge_secret_events
BEGIN SELECT RAISE(ABORT,'edge_secret_event_immutable'); END;
CREATE TRIGGER trg_edge_secret_events_delete_prd123
BEFORE DELETE ON edge_secret_events
BEGIN SELECT RAISE(ABORT,'edge_secret_event_immutable'); END;

CREATE TABLE edge_secret_sync_receipts(
  edge_secret_sync_receipt_id TEXT PRIMARY KEY CHECK(length(edge_secret_sync_receipt_id)=36
    AND substr(edge_secret_sync_receipt_id,1,4)='lsa_'
    AND substr(edge_secret_sync_receipt_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_secret_sync_batch_id TEXT NOT NULL UNIQUE,
  accepted_event_count INTEGER NOT NULL CHECK(accepted_event_count BETWEEN 0 AND 64),
  duplicate_event_count INTEGER NOT NULL CHECK(duplicate_event_count BETWEEN 0 AND 64),
  receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64
    AND receipt_sha256 NOT GLOB '*[^0-9a-f]*'),
  received_at TEXT NOT NULL CHECK(unixepoch(received_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_secret_sync_batch_id)
    REFERENCES edge_secret_sync_batches(
      workspace_id,tenant_id,edge_deployment_id,edge_secret_sync_batch_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_secret_sync_receipt_id),
  CHECK(accepted_event_count+duplicate_event_count BETWEEN 1 AND 64)
);
CREATE TRIGGER trg_edge_secret_sync_receipts_update_prd123
BEFORE UPDATE ON edge_secret_sync_receipts
BEGIN SELECT RAISE(ABORT,'edge_secret_sync_receipt_immutable'); END;
CREATE TRIGGER trg_edge_secret_sync_receipts_delete_prd123
BEFORE DELETE ON edge_secret_sync_receipts
BEGIN SELECT RAISE(ABORT,'edge_secret_sync_receipt_immutable'); END;

CREATE TABLE edge_secret_event_outbox(
  edge_secret_outbox_id TEXT PRIMARY KEY CHECK(length(edge_secret_outbox_id)=36
    AND substr(edge_secret_outbox_id,1,4)='lso_'
    AND substr(edge_secret_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_secret_event_id TEXT NOT NULL,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('workspace','security')),
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','dead_letter')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR(length(last_error_code) BETWEEN 2 AND 64
    AND last_error_code=lower(last_error_code)
    AND last_error_code NOT GLOB '*[^a-z0-9_.]*')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_secret_event_id)
    REFERENCES edge_secret_events(workspace_id,tenant_id,edge_deployment_id,edge_secret_event_id),
  UNIQUE(edge_secret_event_id,delivery_domain)
);
CREATE INDEX idx_edge_secret_outbox_delivery_prd123
  ON edge_secret_event_outbox(status,available_at,edge_secret_outbox_id);
CREATE TRIGGER trg_edge_secret_outbox_update_prd123
BEFORE UPDATE ON edge_secret_event_outbox FOR EACH ROW
WHEN NEW.edge_secret_outbox_id!=OLD.edge_secret_outbox_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.edge_secret_event_id!=OLD.edge_secret_event_id
  OR NEW.delivery_domain!=OLD.delivery_domain OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count<OLD.attempt_count OR NEW.attempt_count>20
  OR NEW.updated_at<=OLD.updated_at
  OR (OLD.status='pending' AND NEW.status NOT IN('processing','dead_letter'))
  OR (OLD.status='processing' AND NEW.status NOT IN('pending','delivered','dead_letter'))
  OR OLD.status IN('delivered','dead_letter')
BEGIN SELECT RAISE(ABORT,'edge_secret_outbox_transition_invalid'); END;
CREATE TRIGGER trg_edge_secret_outbox_delete_prd123
BEFORE DELETE ON edge_secret_event_outbox
BEGIN SELECT RAISE(ABORT,'edge_secret_outbox_immutable'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd123(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-123'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd123_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd123_manifest_sources VALUES
  ('edge_secret_references','delete',NULL),
  ('edge_secret_sync_batches','delete','operational'),
  ('edge_secret_events','archive_then_delete','audit'),
  ('edge_secret_sync_receipts','delete','operational'),
  ('edge_secret_event_outbox','delete','operational');
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
JOIN prd123_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd123(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-123',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd123_manifest_sources ORDER BY source_table;
DROP TABLE prd123_manifest_sources;

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
CREATE TRIGGER trg_tenant_deletion_manifest_prd123_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd123
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd123_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd123
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd123_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd123
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd123_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd123_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_edge_secret_permission_extensions)=1
      AND (SELECT COUNT(*) FROM workspace_edge_secret_role_permissions)=4
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd123)=5
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_secret_permission_extensions','workspace_edge_secret_role_permissions',
      'edge_secret_references','edge_secret_sync_batches','edge_secret_events',
      'edge_secret_sync_receipts','edge_secret_event_outbox',
      'tenant_deletion_manifest_extensions_prd123'))=8 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd123_up_final_guard;
COMMIT;
