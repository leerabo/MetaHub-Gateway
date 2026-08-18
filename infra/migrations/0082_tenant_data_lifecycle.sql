PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- PRD-089 is intentionally anchored to the completed PRD-088 schema.
CREATE TEMP TABLE prd089_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd089_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0081' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=98
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=23)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='fcda2ee936d9a04c1f8f469ef204ac9fdf04aa7c646a895e93d8b5d7dd2f0350'
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd089')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd089_up_guard;

CREATE TABLE tenant_export_jobs(
  tenant_export_job_id TEXT PRIMARY KEY CHECK(length(tenant_export_job_id)=36
    AND substr(tenant_export_job_id,1,4)='tex_'
    AND substr(tenant_export_job_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  requested_by_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  idempotency_key_hash TEXT NOT NULL CHECK(length(idempotency_key_hash)=64
    AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'),
  request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint)=64
    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('pending','processing','completed','failed','expired')),
  schema_version TEXT NOT NULL CHECK(schema_version='metahub.tenant-export/v1'),
  snapshot_at TEXT NOT NULL CHECK(unixepoch(snapshot_at) IS NOT NULL),
  usage_snapshot_sequence INTEGER NOT NULL DEFAULT 0 CHECK(usage_snapshot_sequence>=0),
  audit_snapshot_sequence INTEGER NOT NULL DEFAULT 0 CHECK(audit_snapshot_sequence>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 3),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
  manifest_json TEXT CHECK(manifest_json IS NULL OR(json_valid(manifest_json)
    AND json_type(manifest_json)='object' AND length(CAST(manifest_json AS BLOB))<=65536)),
  content_ciphertext BLOB,
  content_key_version INTEGER,
  content_size INTEGER,
  content_sha256 TEXT,
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  download_count INTEGER NOT NULL DEFAULT 0 CHECK(download_count>=0),
  error_code TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(tenant_id,requested_by_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,tenant_export_job_id),
  UNIQUE(tenant_id,requested_by_user_id,request_id),
  UNIQUE(tenant_id,requested_by_user_id,idempotency_key_hash),
  CHECK((status='pending' AND attempt_count=0 AND lease_owner_id IS NULL
      AND lease_token IS NULL AND lease_expires_at IS NULL AND content_ciphertext IS NULL)
    OR(status='processing' AND attempt_count BETWEEN 1 AND 3 AND lease_owner_id IS NOT NULL
      AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL AND content_ciphertext IS NULL)
    OR(status='completed' AND completed_at IS NOT NULL AND manifest_json IS NOT NULL
      AND content_ciphertext IS NOT NULL AND content_key_version IS NOT NULL
      AND content_size IS NOT NULL AND content_sha256 IS NOT NULL)
    OR(status IN('failed','expired') AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND content_ciphertext IS NULL)),
  CHECK(content_sha256 IS NULL OR(length(content_sha256)=64
    AND content_sha256 NOT GLOB '*[^0-9a-f]*'))
);
CREATE INDEX idx_tenant_export_jobs_claim_prd089
  ON tenant_export_jobs(status,lease_expires_at,created_at,tenant_export_job_id);
CREATE INDEX idx_tenant_export_jobs_owner_prd089
  ON tenant_export_jobs(tenant_id,created_at DESC,tenant_export_job_id);

CREATE TABLE deletion_requests(
  deletion_request_id TEXT PRIMARY KEY CHECK(length(deletion_request_id)=36
    AND substr(deletion_request_id,1,4)='del_'
    AND substr(deletion_request_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  actor_id_hash TEXT NOT NULL CHECK(length(actor_id_hash)=64
    AND actor_id_hash NOT GLOB '*[^0-9a-f]*'),
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  idempotency_key_hash TEXT NOT NULL CHECK(length(idempotency_key_hash)=64
    AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'),
  request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint)=64
    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('requested','quiescing','deleting','completed','failed')),
  tenant_revision INTEGER NOT NULL CHECK(tenant_revision>=1),
  confirmation_hash TEXT NOT NULL CHECK(length(confirmation_hash)=64
    AND confirmation_hash NOT GLOB '*[^0-9a-f]*'),
  retention_policy_version INTEGER NOT NULL CHECK(retention_policy_version>=1),
  schema_manifest_sha256 TEXT NOT NULL CHECK(length(schema_manifest_sha256)=64
    AND schema_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  archived_rows INTEGER NOT NULL DEFAULT 0 CHECK(archived_rows>=0),
  deleted_rows INTEGER NOT NULL DEFAULT 0 CHECK(deleted_rows>=0),
  retained_records INTEGER NOT NULL DEFAULT 0 CHECK(retained_records>=0),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT,
  cache_invalidated_at TEXT,
  completed_at TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE(tenant_id,deletion_request_id), UNIQUE(tenant_id,request_id),
  UNIQUE(tenant_id,idempotency_key_hash),
  CHECK(status!='completed' OR(completed_at IS NOT NULL AND cache_invalidated_at IS NOT NULL)),
  CHECK((status IN('requested','completed','failed') AND lease_owner_id IS NULL
      AND lease_token IS NULL AND lease_expires_at IS NULL)
    OR(status IN('quiescing','deleting') AND lease_owner_id IS NOT NULL
      AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))
);
CREATE UNIQUE INDEX idx_deletion_requests_one_active_prd089
  ON deletion_requests(tenant_id) WHERE status IN('requested','quiescing','deleting');
CREATE INDEX idx_deletion_requests_claim_prd089
  ON deletion_requests(status,lease_expires_at,created_at,deletion_request_id);

CREATE TABLE tenant_data_states(
  tenant_id TEXT PRIMARY KEY,
  lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN('active','deletion_requested','deleted')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  cache_generation INTEGER NOT NULL DEFAULT 0 CHECK(cache_generation>=0),
  active_deletion_request_id TEXT,
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(tenant_id,active_deletion_request_id)
    REFERENCES deletion_requests(tenant_id,deletion_request_id) DEFERRABLE INITIALLY DEFERRED,
  CHECK((lifecycle_status='active' AND active_deletion_request_id IS NULL)
    OR(lifecycle_status IN('deletion_requested','deleted') AND active_deletion_request_id IS NOT NULL))
);

CREATE TABLE tenant_retention_archives(
  tenant_retention_archive_id TEXT PRIMARY KEY CHECK(length(tenant_retention_archive_id)=36
    AND substr(tenant_retention_archive_id,1,4)='tra_'
    AND substr(tenant_retention_archive_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL, deletion_request_id TEXT NOT NULL,
  source_table TEXT NOT NULL CHECK(length(source_table) BETWEEN 1 AND 128
    AND source_table NOT GLOB '*[^A-Za-z0-9_]*'),
  segment_no INTEGER NOT NULL CHECK(segment_no>=0),
  schema_version TEXT NOT NULL CHECK(schema_version='metahub.tenant-retention/v1'),
  retention_class TEXT NOT NULL CHECK(retention_class IN('audit','usage','billing','finops','security','operational')),
  row_count INTEGER NOT NULL CHECK(row_count>=0), uncompressed_size INTEGER NOT NULL CHECK(uncompressed_size>=0),
  compressed_size INTEGER NOT NULL CHECK(compressed_size>=0), content_ciphertext BLOB NOT NULL,
  content_key_version INTEGER NOT NULL CHECK(content_key_version>=1),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
  retain_until TEXT NOT NULL CHECK(unixepoch(retain_until) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,deletion_request_id) REFERENCES deletion_requests(tenant_id,deletion_request_id),
  UNIQUE(tenant_id,deletion_request_id,source_table,segment_no)
);
CREATE INDEX idx_tenant_retention_owner_prd089
  ON tenant_retention_archives(tenant_id,deletion_request_id,source_table,segment_no);

CREATE TABLE tenant_lifecycle_events(
  tenant_lifecycle_event_id TEXT PRIMARY KEY CHECK(length(tenant_lifecycle_event_id)=36
    AND substr(tenant_lifecycle_event_id,1,4)='tle_'
    AND substr(tenant_lifecycle_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN('tenant.exported','tenant.deletion_requested')),
  object_id TEXT NOT NULL, actor_id_hash TEXT NOT NULL CHECK(length(actor_id_hash)=64
    AND actor_id_hash NOT GLOB '*[^0-9a-f]*'),
  request_id TEXT NOT NULL, trace_id TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=8192),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE(tenant_id,tenant_lifecycle_event_id), UNIQUE(tenant_id,event_type,object_id),
  CHECK(payload_sha256=metahub_sha256(payload_json))
);
CREATE INDEX idx_tenant_lifecycle_events_owner_prd089
  ON tenant_lifecycle_events(tenant_id,created_at,tenant_lifecycle_event_id);

CREATE TABLE tenant_lifecycle_event_outbox(
  tenant_lifecycle_event_outbox_id TEXT PRIMARY KEY CHECK(length(tenant_lifecycle_event_outbox_id)=36
    AND substr(tenant_lifecycle_event_outbox_id,1,4)='tlo_'
    AND substr(tenant_lifecycle_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL, tenant_lifecycle_event_id TEXT NOT NULL, webhook_event_id TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('pending','leased','delivered','no_subscription','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 10),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0), lease_owner_id TEXT, lease_token TEXT,
  lease_expires_at TEXT, next_attempt_at TEXT, last_error_code TEXT,
  subscription_snapshot_json TEXT NOT NULL CHECK(json_valid(subscription_snapshot_json)
    AND json_type(subscription_snapshot_json)='array'),
  snapshot_frozen_at TEXT NOT NULL CHECK(unixepoch(snapshot_frozen_at) IS NOT NULL), delivered_at TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,tenant_lifecycle_event_id)
    REFERENCES tenant_lifecycle_events(tenant_id,tenant_lifecycle_event_id),
  UNIQUE(tenant_id,tenant_lifecycle_event_id), UNIQUE(tenant_id,webhook_event_id),
  CHECK(payload_sha256=metahub_sha256(payload_json)),
  CHECK((status='pending' AND attempt_count BETWEEN 0 AND 9 AND next_attempt_at IS NOT NULL
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
    OR(status='leased' AND attempt_count BETWEEN 1 AND 10 AND next_attempt_at IS NULL
      AND lease_owner_id IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    OR(status IN('delivered','no_subscription') AND delivered_at IS NOT NULL
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
    OR(status='failed' AND attempt_count=10 AND delivered_at IS NULL
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL))
);
CREATE INDEX idx_tenant_lifecycle_outbox_claim_prd089
  ON tenant_lifecycle_event_outbox(status,next_attempt_at,lease_expires_at,created_at);

CREATE TABLE tenant_deletion_table_manifest(
  source_table TEXT PRIMARY KEY CHECK(length(source_table) BETWEEN 1 AND 128
    AND source_table NOT GLOB '*[^A-Za-z0-9_]*'),
  disposition TEXT NOT NULL CHECK(disposition IN('delete','archive_then_delete','preserve','root')),
  retention_class TEXT CHECK(retention_class IS NULL OR retention_class IN('audit','usage','billing','finops','security','operational')),
  table_sql_sha256 TEXT NOT NULL CHECK(length(table_sql_sha256)=64 AND table_sql_sha256 NOT GLOB '*[^0-9a-f]*'),
  column_signature_sha256 TEXT NOT NULL CHECK(length(column_signature_sha256)=64 AND column_signature_sha256 NOT GLOB '*[^0-9a-f]*'),
  trigger_set_sha256 TEXT NOT NULL CHECK(length(trigger_set_sha256)=64 AND trigger_set_sha256 NOT GLOB '*[^0-9a-f]*'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1), created_at TEXT NOT NULL
);
CREATE TABLE tenant_deletion_trigger_manifest(
  trigger_name TEXT PRIMARY KEY CHECK(length(trigger_name) BETWEEN 1 AND 128 AND trigger_name NOT GLOB '*[^A-Za-z0-9_]*'),
  source_table TEXT NOT NULL, trigger_sql TEXT NOT NULL,
  trigger_sha256 TEXT NOT NULL CHECK(length(trigger_sha256)=64 AND trigger_sha256 NOT GLOB '*[^0-9a-f]*'),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);

CREATE TRIGGER trg_tenant_export_jobs_owner_prd089 BEFORE INSERT ON tenant_export_jobs FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM users WHERE tenant_id=NEW.tenant_id AND user_id=NEW.requested_by_user_id AND status='active')
BEGIN SELECT RAISE(ABORT,'tenant_export_owner_mismatch'); END;
CREATE TRIGGER trg_tenant_export_jobs_transition_prd089 BEFORE UPDATE ON tenant_export_jobs FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.tenant_export_job_id!=OLD.tenant_export_job_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.requested_by_user_id!=OLD.requested_by_user_id OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.idempotency_key_hash!=OLD.idempotency_key_hash OR NEW.request_fingerprint!=OLD.request_fingerprint
  OR NEW.schema_version!=OLD.schema_version OR NEW.snapshot_at!=OLD.snapshot_at OR NEW.created_at!=OLD.created_at
  OR NEW.generation<=OLD.generation OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'tenant_export_transition_invalid'); END;
CREATE TRIGGER trg_tenant_export_jobs_delete_prd089 BEFORE DELETE ON tenant_export_jobs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_export_immutable'); END;
CREATE TRIGGER trg_deletion_requests_owner_prd089 BEFORE INSERT ON deletion_requests FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id AND status='active')
BEGIN SELECT RAISE(ABORT,'tenant_deletion_owner_mismatch'); END;
CREATE TRIGGER trg_deletion_requests_transition_prd089 BEFORE UPDATE ON deletion_requests FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.deletion_request_id!=OLD.deletion_request_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.actor_id_hash!=OLD.actor_id_hash OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.idempotency_key_hash!=OLD.idempotency_key_hash OR NEW.request_fingerprint!=OLD.request_fingerprint
  OR NEW.tenant_revision!=OLD.tenant_revision OR NEW.confirmation_hash!=OLD.confirmation_hash
  OR NEW.schema_manifest_sha256!=OLD.schema_manifest_sha256 OR NEW.created_at!=OLD.created_at
  OR NEW.generation<=OLD.generation OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'tenant_deletion_transition_invalid'); END;
CREATE TRIGGER trg_deletion_requests_delete_prd089 BEFORE DELETE ON deletion_requests FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_immutable'); END;
CREATE TRIGGER trg_tenant_data_states_transition_prd089 BEFORE UPDATE ON tenant_data_states FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.tenant_id!=OLD.tenant_id OR NEW.revision!=OLD.revision+1
  OR NEW.cache_generation<OLD.cache_generation OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'tenant_data_state_transition_invalid'); END;
CREATE TRIGGER trg_tenant_data_states_delete_prd089 BEFORE DELETE ON tenant_data_states FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_data_state_immutable'); END;
CREATE TRIGGER trg_tenant_retention_archives_update_prd089 BEFORE UPDATE ON tenant_retention_archives FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_retention_immutable'); END;
CREATE TRIGGER trg_tenant_retention_archives_delete_prd089 BEFORE DELETE ON tenant_retention_archives FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_retention_immutable'); END;
CREATE TRIGGER trg_tenant_lifecycle_events_update_prd089 BEFORE UPDATE ON tenant_lifecycle_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_lifecycle_event_immutable'); END;
CREATE TRIGGER trg_tenant_lifecycle_events_delete_prd089 BEFORE DELETE ON tenant_lifecycle_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_lifecycle_event_immutable'); END;
CREATE TRIGGER trg_tenant_lifecycle_outbox_transition_prd089 BEFORE UPDATE ON tenant_lifecycle_event_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.tenant_lifecycle_event_outbox_id!=OLD.tenant_lifecycle_event_outbox_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.tenant_lifecycle_event_id!=OLD.tenant_lifecycle_event_id
  OR NEW.webhook_event_id!=OLD.webhook_event_id OR NEW.payload_json!=OLD.payload_json
  OR NEW.payload_sha256!=OLD.payload_sha256 OR NEW.subscription_snapshot_json!=OLD.subscription_snapshot_json
  OR NEW.snapshot_frozen_at!=OLD.snapshot_frozen_at OR NEW.created_at!=OLD.created_at
  OR NEW.generation<=OLD.generation OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'tenant_lifecycle_outbox_transition_invalid'); END;
CREATE TRIGGER trg_tenant_lifecycle_outbox_delete_prd089 BEFORE DELETE ON tenant_lifecycle_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_lifecycle_outbox_immutable'); END;

INSERT INTO tenant_data_states(tenant_id,lifecycle_status,revision,cache_generation,active_deletion_request_id,updated_at)
SELECT tenant_id,'active',1,0,NULL,strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM tenants;
CREATE TRIGGER trg_tenants_tenant_data_initialize_prd089 AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  INSERT INTO tenant_data_states(tenant_id,lifecycle_status,revision,cache_generation,active_deletion_request_id,updated_at)
  VALUES(NEW.tenant_id,'active',1,0,NULL,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
END;

-- Audit enums are expanded with writable_schema so the historical owner triggers stay intact.
CREATE TEMP TABLE prd089_audit_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd089_audit_guard(issue_count)
SELECT CASE WHEN instr(sql,'''branding.updated''')>0 AND instr(sql,'''tenant.exported''')=0
    AND instr(sql,'''branding_config''')>0 AND instr(sql,'''tenant_export_job''')=0 THEN 0 ELSE 1 END
FROM sqlite_schema WHERE type='table' AND name='audit_logs';
DROP TABLE prd089_audit_guard;
PRAGMA writable_schema=ON;
UPDATE sqlite_master SET sql=replace(replace(sql,'''branding.updated''',
  '''branding.updated'',''tenant.export_requested'',''tenant.exported'',''tenant.deletion_requested'',''tenant.deleted'''),
  '''branding_config''','''branding_config'',''tenant_export_job'',''deletion_request''')
WHERE type='table' AND name='audit_logs';
PRAGMA schema_version=1;
PRAGMA writable_schema=OFF;

-- Revision 24 keeps customized role matrices untouched and expands only the published defaults.
CREATE TEMP TABLE prd089_roles_to_expand AS
SELECT role_id,tenant_id,code FROM roles WHERE code IN('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  AND matrix_sha256=CASE code
    WHEN 'admin' THEN 'b9036b7e02f9f5d7dc282bb87bdd5f6312eab239d4159aca53ae34e82e90ff5e'
    WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
    WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e'
    WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
    WHEN 'security_admin' THEN 'eba36b2011e994eda4d1f30368b5c53c580b025f28c3a3dee25e564a4a586377'
    WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END;
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
DROP TRIGGER trg_role_permissions_delete_prd059;
DROP TRIGGER trg_rbac_permission_templates_insert_prd059;
DROP TRIGGER trg_rbac_permission_templates_update_prd059;
DROP TRIGGER trg_rbac_permission_templates_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;
CREATE TABLE rbac_permission_templates_prd089_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80 AND permission_code=lower(permission_code) AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40), name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300), risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=24));
INSERT INTO rbac_permission_templates_prd089_new(rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,24 FROM rbac_permission_templates;
INSERT INTO rbac_permission_templates_prd089_new VALUES
('tenant_data.read','tenant','读取租户数据生命周期','读取当前租户导出任务、注销状态与安全留存摘要。','read',24),
('tenant_data.export','tenant','创建租户数据导出','创建当前租户不含凭据、Prompt 和 Response 的受保护数据导出。','critical',24),
('tenant_data.download','tenant','下载租户数据导出','下载当前租户已完成且未过期的加密存储导出制品。','critical',24),
('tenant_data.delete','tenant','申请租户注销','冻结当前租户并启动可恢复的数据归档、缓存失效和业务数据擦除流程。','critical',24);
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd089_new RENAME TO rbac_permission_templates;
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
SELECT 'admin',permission_code,1 FROM rbac_permission_templates WHERE permission_code LIKE 'tenant_data.%';
INSERT INTO rbac_role_permission_templates VALUES('security_admin','tenant_data.read',1);
CREATE TABLE permissions_prd089_new(
  permission_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, code TEXT NOT NULL, category TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')), catalog_revision INTEGER NOT NULL CHECK(catalog_revision=24), status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id), FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code), UNIQUE(tenant_id,permission_id));
INSERT INTO permissions_prd089_new(rowid,permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,24,status,created_at,updated_at FROM permissions;
INSERT INTO permissions_prd089_new
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,template.category,template.name,template.description,template.risk_level,24,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template ON template.permission_code LIKE 'tenant_data.%';
DROP TABLE permissions;
ALTER TABLE permissions_prd089_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);
INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd089_roles_to_expand role JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE (role.code='admin' AND permission.code LIKE 'tenant_data.%') OR (role.code='security_admin' AND permission.code='tenant_data.read');
UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=CASE code
  WHEN 'admin' THEN 'd64ef0db03b06138b5a5b5d4652ccdfcad2c79b420b3ce04fd5772eeebbf0ab1'
  WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
  WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e'
  WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
  WHEN 'security_admin' THEN 'f0f642cea1be202d82016ac045332c729acd04060be8e5de68b32a597d57716b'
  WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END
WHERE role_id IN(SELECT role_id FROM prd089_roles_to_expand);
CREATE TRIGGER trg_rbac_permission_templates_insert_prd059 BEFORE INSERT ON rbac_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_update_prd059 BEFORE UPDATE ON rbac_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_delete_prd059 BEFORE DELETE ON rbac_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_insert_prd059 BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_update_prd059 BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_delete_prd059 BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_permissions_owner_insert_prd059 BEFORE INSERT ON permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM rbac_permission_templates t WHERE t.permission_code=NEW.code AND t.category=NEW.category AND t.name=NEW.name AND t.description=NEW.description AND t.risk_level=NEW.risk_level AND t.catalog_revision=NEW.catalog_revision) OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.status!='active' OR EXISTS(SELECT 1 FROM permissions p WHERE p.permission_id=NEW.permission_id OR(p.tenant_id=NEW.tenant_id AND p.code=NEW.code) OR(NEW.rowid!=-1 AND p.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_update_prd059 BEFORE UPDATE ON permissions FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_delete_prd059 BEFORE DELETE ON permissions FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_rowid_insert_prd059 AFTER INSERT ON permissions FOR EACH ROW WHEN NEW.rowid<0 BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_roles_owner_insert_prd059 BEFORE INSERT ON roles FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision!=1 OR NEW.matrix_sha256!=CASE NEW.code WHEN 'admin' THEN 'd64ef0db03b06138b5a5b5d4652ccdfcad2c79b420b3ce04fd5772eeebbf0ab1' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN 'f0f642cea1be202d82016ac045332c729acd04060be8e5de68b32a597d57716b' WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059 BEFORE UPDATE ON roles FOR EACH ROW WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*' BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059 BEFORE INSERT ON role_permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM roles r JOIN permissions p ON p.tenant_id=r.tenant_id JOIN rbac_role_permission_templates t ON t.role_code=r.code AND t.permission_code=p.code WHERE r.tenant_id=NEW.tenant_id AND r.role_id=NEW.role_id AND r.status='active' AND p.permission_id=NEW.permission_id AND p.status='active') OR EXISTS(SELECT 1 FROM role_permissions x WHERE x.role_permission_id=NEW.role_permission_id OR(x.tenant_id=NEW.tenant_id AND x.role_id=NEW.role_id AND x.permission_id=NEW.permission_id) OR(NEW.rowid!=-1 AND x.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059 BEFORE DELETE ON role_permissions FOR EACH ROW WHEN EXISTS(SELECT 1 FROM roles r WHERE r.tenant_id=OLD.tenant_id AND r.role_id=OLD.role_id AND r.code='admin') BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059 AFTER INSERT ON tenants FOR EACH ROW BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,c.role_code,c.role_code,'PRD-089 fixed role','active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,CASE c.role_code WHEN 'admin' THEN 'd64ef0db03b06138b5a5b5d4652ccdfcad2c79b420b3ce04fd5772eeebbf0ab1' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN 'f0f642cea1be202d82016ac045332c729acd04060be8e5de68b32a597d57716b' WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,24,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,r.role_id,p.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
END;

-- Store a signed description of every tenant-scoped table and trigger.  Runtime code refuses
-- to delete when this inventory no longer matches sqlite_schema.
INSERT INTO tenant_deletion_table_manifest(source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,trigger_set_sha256,catalog_revision,created_at)
SELECT s.name,CASE WHEN s.name='tenants' THEN 'root' WHEN s.name IN('tenant_data_states','deletion_requests','tenant_retention_archives','tenant_lifecycle_events','tenant_lifecycle_event_outbox') THEN 'preserve' WHEN s.name IN('audit_logs','usage_ledger','finops_ledger_entries','finops_adjustments','billing_disputes','billing_dispute_items','billing_dispute_transitions','billing_dispute_adjustment_links','billing_settlement_holds','security_event_sources','security_event_aggregates','security_event_aggregate_members') THEN 'archive_then_delete' ELSE 'delete' END,CASE WHEN s.name='audit_logs' THEN 'audit' WHEN s.name='usage_ledger' THEN 'usage' WHEN s.name LIKE 'finops_%' THEN 'finops' WHEN s.name LIKE 'billing_%' THEN 'billing' WHEN s.name LIKE 'security_event_%' THEN 'security' ELSE NULL END,metahub_sha256(s.sql),metahub_sha256((SELECT group_concat(record,char(30)) FROM(SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)||coalesce(dflt_value,'')||char(31)||pk AS record FROM pragma_table_info(s.name) ORDER BY cid))),metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(SELECT t.name||char(31)||metahub_sha256(t.sql) AS record FROM sqlite_schema t WHERE t.type='trigger' AND t.tbl_name=s.name ORDER BY t.name)),'')),1,strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM sqlite_schema s WHERE s.type='table' AND s.name NOT LIKE 'sqlite_%' AND EXISTS(SELECT 1 FROM pragma_table_info(s.name) WHERE name='tenant_id') ORDER BY s.name;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT t.name,t.tbl_name,t.sql,metahub_sha256(t.sql) FROM sqlite_schema t JOIN tenant_deletion_table_manifest m ON m.source_table=t.tbl_name WHERE t.type='trigger' ORDER BY t.tbl_name,t.name;
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089 BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089 BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd089_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd089_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM tenant_data_states)=(SELECT COUNT(*) FROM tenants) THEN 0 ELSE 1 END
 + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=102 AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=24) AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||char(31)||risk_level||char(31)||catalog_revision AS record FROM rbac_permission_templates ORDER BY permission_code))='96fa2542d731eb2f87b246a18a935aba1dbfe28017417d0f43c720e1f0c81c74' THEN 0 ELSE 1 END
 + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=259 AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record FROM rbac_role_permission_templates ORDER BY role_code,permission_code))='8ade3424d9ccd4b324ed699ad36c96c0fc30a33694af75de4d02009dbdb76975' THEN 0 ELSE 1 END
 + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN('tenant_export_jobs','deletion_requests','tenant_data_states','tenant_retention_archives','tenant_lifecycle_events','tenant_lifecycle_event_outbox','tenant_deletion_table_manifest','tenant_deletion_trigger_manifest'))=8 THEN 0 ELSE 1 END
 + (SELECT COUNT(*) FROM pragma_foreign_key_check)+(SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd089_final_guard;
DROP TABLE prd089_roles_to_expand;
COMMIT;
PRAGMA foreign_keys = ON;
PRAGMA legacy_alter_table = OFF;
