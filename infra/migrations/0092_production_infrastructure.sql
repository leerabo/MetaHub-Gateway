PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd100_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd100_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0091' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_roles','platform_operators','workspaces','workspace_tenant_bindings'))=4
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd100')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd100_up_guard;

CREATE TABLE platform_infrastructure_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.infrastructure.read','platform.infrastructure.manage',
    'platform.backup.create','platform.key_security.read')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-100'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=4)
);
CREATE TABLE platform_infrastructure_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_infrastructure_permission_extensions(permission_code)
);
INSERT INTO platform_infrastructure_permission_extensions VALUES
  ('platform.infrastructure.read','PRD-100',4),
  ('platform.infrastructure.manage','PRD-100',4),
  ('platform.backup.create','PRD-100',4),
  ('platform.key_security.read','PRD-100',4);
INSERT INTO platform_infrastructure_role_permissions VALUES
  ('platform_owner','platform.infrastructure.read'),
  ('platform_owner','platform.infrastructure.manage'),
  ('platform_owner','platform.backup.create'),
  ('platform_owner','platform.key_security.read'),
  ('platform_ops','platform.infrastructure.read'),
  ('platform_ops','platform.infrastructure.manage'),
  ('platform_ops','platform.backup.create'),
  ('platform_security','platform.infrastructure.read'),
  ('platform_security','platform.key_security.read');
CREATE TRIGGER trg_platform_infrastructure_permissions_insert_prd100
BEFORE INSERT ON platform_infrastructure_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_infrastructure_permission_immutable'); END;
CREATE TRIGGER trg_platform_infrastructure_permissions_update_prd100
BEFORE UPDATE ON platform_infrastructure_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_infrastructure_permission_immutable'); END;
CREATE TRIGGER trg_platform_infrastructure_permissions_delete_prd100
BEFORE DELETE ON platform_infrastructure_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_infrastructure_permission_immutable'); END;
CREATE TRIGGER trg_platform_infrastructure_role_permissions_insert_prd100
BEFORE INSERT ON platform_infrastructure_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_infrastructure_permission_immutable'); END;
CREATE TRIGGER trg_platform_infrastructure_role_permissions_update_prd100
BEFORE UPDATE ON platform_infrastructure_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_infrastructure_permission_immutable'); END;
CREATE TRIGGER trg_platform_infrastructure_role_permissions_delete_prd100
BEFORE DELETE ON platform_infrastructure_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_infrastructure_permission_immutable'); END;

CREATE TABLE infrastructure_resources(
  infrastructure_resource_id TEXT PRIMARY KEY CHECK(
    length(infrastructure_resource_id)=36
    AND substr(infrastructure_resource_id,1,4)='ifr_'),
  resource_kind TEXT NOT NULL UNIQUE CHECK(resource_kind IN(
    'database_cluster','cache_cluster','object_store','key_ring')),
  backend_code TEXT NOT NULL CHECK(backend_code IN(
    'postgresql','redis','s3_compatible','kms')),
  desired_status TEXT NOT NULL CHECK(desired_status='active'),
  observed_status TEXT NOT NULL CHECK(observed_status IN(
    'unknown','healthy','degraded','unavailable','not_configured')),
  version_summary TEXT CHECK(version_summary IS NULL
    OR length(version_summary) BETWEEN 1 AND 80),
  latency_ms INTEGER CHECK(latency_ms IS NULL OR latency_ms BETWEEN 0 AND 300000),
  last_checked_at TEXT CHECK(last_checked_at IS NULL OR unixepoch(last_checked_at) IS NOT NULL),
  last_verified_at TEXT CHECK(last_verified_at IS NULL OR unixepoch(last_verified_at) IS NOT NULL),
  error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 3 AND 96),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK((resource_kind='database_cluster' AND backend_code='postgresql')
    OR(resource_kind='cache_cluster' AND backend_code='redis')
    OR(resource_kind='object_store' AND backend_code='s3_compatible')
    OR(resource_kind='key_ring' AND backend_code='kms')),
  CHECK((observed_status IN('healthy','unknown','not_configured') AND error_code IS NULL)
    OR(observed_status IN('degraded','unavailable') AND error_code IS NOT NULL))
);
INSERT INTO infrastructure_resources VALUES
  ('ifr_00000000000000000000000000000001','database_cluster','postgresql','active',
   'unknown',NULL,NULL,NULL,NULL,NULL,1,datetime('now'),datetime('now')),
  ('ifr_00000000000000000000000000000002','cache_cluster','redis','active',
   'unknown',NULL,NULL,NULL,NULL,NULL,1,datetime('now'),datetime('now')),
  ('ifr_00000000000000000000000000000003','object_store','s3_compatible','active',
   'unknown',NULL,NULL,NULL,NULL,NULL,1,datetime('now'),datetime('now')),
  ('ifr_00000000000000000000000000000004','key_ring','kms','active',
   'unknown',NULL,NULL,NULL,NULL,NULL,1,datetime('now'),datetime('now'));
CREATE TRIGGER trg_infrastructure_resource_owner_update_prd100
BEFORE UPDATE ON infrastructure_resources FOR EACH ROW
WHEN NEW.infrastructure_resource_id!=OLD.infrastructure_resource_id
  OR NEW.resource_kind!=OLD.resource_kind OR NEW.backend_code!=OLD.backend_code
  OR NEW.desired_status!=OLD.desired_status OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'infrastructure_resource_owner_immutable'); END;
CREATE TRIGGER trg_infrastructure_resources_delete_prd100
BEFORE DELETE ON infrastructure_resources
BEGIN SELECT RAISE(ABORT,'infrastructure_resource_immutable'); END;

CREATE TABLE migration_leases(
  migration_lease_id TEXT PRIMARY KEY CHECK(length(migration_lease_id)=36
    AND substr(migration_lease_id,1,4)='mgl_'),
  backend_code TEXT NOT NULL CHECK(backend_code IN('sqlite','postgresql')),
  lease_name TEXT NOT NULL CHECK(length(lease_name) BETWEEN 3 AND 96),
  owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 8 AND 128),
  lease_token_sha256 TEXT NOT NULL CHECK(length(lease_token_sha256)=64),
  generation INTEGER NOT NULL CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN('active','released','expired')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  acquired_at TEXT NOT NULL CHECK(unixepoch(acquired_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  UNIQUE(backend_code,lease_name,generation),
  CHECK((status='active' AND released_at IS NULL)
    OR(status IN('released','expired') AND released_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_migration_leases_active_prd100
  ON migration_leases(backend_code,lease_name) WHERE status='active';
CREATE INDEX idx_migration_leases_expiry_prd100
  ON migration_leases(status,expires_at,migration_lease_id);
CREATE TRIGGER trg_migration_lease_owner_update_prd100
BEFORE UPDATE ON migration_leases FOR EACH ROW
WHEN NEW.migration_lease_id!=OLD.migration_lease_id
  OR NEW.backend_code!=OLD.backend_code OR NEW.lease_name!=OLD.lease_name
  OR NEW.owner_id!=OLD.owner_id OR NEW.lease_token_sha256!=OLD.lease_token_sha256
  OR NEW.generation!=OLD.generation OR NEW.acquired_at!=OLD.acquired_at
BEGIN SELECT RAISE(ABORT,'migration_lease_owner_immutable'); END;
CREATE TRIGGER trg_migration_leases_delete_prd100
BEFORE DELETE ON migration_leases
BEGIN SELECT RAISE(ABORT,'migration_lease_immutable'); END;

CREATE TABLE backup_sets(
  backup_id TEXT PRIMARY KEY CHECK(length(backup_id)=36 AND substr(backup_id,1,4)='bkp_'),
  backup_type TEXT NOT NULL CHECK(backup_type IN('full','incremental')),
  scope_kind TEXT NOT NULL CHECK(scope_kind IN('platform','workspace')),
  workspace_id TEXT,
  tenant_id TEXT,
  status TEXT NOT NULL CHECK(status IN(
    'requested','snapshotting','uploading','verifying','ready','failed')),
  lease_owner_id TEXT CHECK(lease_owner_id IS NULL OR length(lease_owner_id) BETWEEN 8 AND 128),
  lease_token_sha256 TEXT CHECK(lease_token_sha256 IS NULL OR length(lease_token_sha256)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL
    OR unixepoch(lease_expires_at) IS NOT NULL),
  source_backend TEXT NOT NULL CHECK(source_backend IN('sqlite','postgresql')),
  source_watermark TEXT CHECK(source_watermark IS NULL OR length(source_watermark)<=256),
  object_key TEXT CHECK(object_key IS NULL OR length(object_key) BETWEEN 16 AND 1024),
  size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes BETWEEN 0 AND 10995116277760),
  content_sha256 TEXT CHECK(content_sha256 IS NULL OR length(content_sha256)=64),
  manifest_sha256 TEXT CHECK(manifest_sha256 IS NULL OR length(manifest_sha256)=64),
  kms_key_version TEXT CHECK(kms_key_version IS NULL
    OR length(kms_key_version) BETWEEN 1 AND 128),
  schema_head TEXT CHECK(schema_head IS NULL OR length(schema_head)=4),
  requested_by_operator_id TEXT NOT NULL,
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 3 AND 96),
  retention_until TEXT NOT NULL CHECK(unixepoch(retention_until) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(requested_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(requested_by_operator_id,idempotency_sha256),
  CHECK((scope_kind='platform' AND workspace_id IS NULL AND tenant_id IS NULL)
    OR(scope_kind='workspace' AND workspace_id IS NOT NULL AND tenant_id IS NOT NULL)),
  CHECK((status IN('snapshotting','uploading','verifying')
      AND lease_owner_id IS NOT NULL AND lease_token_sha256 IS NOT NULL
      AND lease_expires_at IS NOT NULL)
    OR(status IN('requested','ready','failed') AND lease_owner_id IS NULL
      AND lease_token_sha256 IS NULL AND lease_expires_at IS NULL)),
  CHECK((status='failed' AND error_code IS NOT NULL AND completed_at IS NOT NULL)
    OR(status='ready' AND error_code IS NULL AND completed_at IS NOT NULL
      AND object_key IS NOT NULL AND content_sha256 IS NOT NULL
      AND manifest_sha256 IS NOT NULL AND kms_key_version IS NOT NULL
      AND schema_head IS NOT NULL)
    OR(status IN('requested','snapshotting','uploading','verifying')
      AND error_code IS NULL AND completed_at IS NULL))
);
CREATE INDEX idx_backup_sets_status_prd100
  ON backup_sets(status,created_at,backup_id);
CREATE INDEX idx_backup_sets_workspace_prd100
  ON backup_sets(workspace_id,tenant_id,created_at,backup_id);
CREATE TRIGGER trg_backup_set_owner_update_prd100
BEFORE UPDATE ON backup_sets FOR EACH ROW
WHEN NEW.backup_id!=OLD.backup_id OR NEW.backup_type!=OLD.backup_type
  OR NEW.scope_kind!=OLD.scope_kind OR NEW.workspace_id IS NOT OLD.workspace_id
  OR NEW.tenant_id IS NOT OLD.tenant_id OR NEW.source_backend!=OLD.source_backend
  OR NEW.requested_by_operator_id!=OLD.requested_by_operator_id
  OR NEW.idempotency_sha256!=OLD.idempotency_sha256
  OR NEW.request_sha256!=OLD.request_sha256 OR NEW.request_id!=OLD.request_id
  OR NEW.trace_id!=OLD.trace_id OR NEW.retention_until!=OLD.retention_until
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'backup_set_owner_immutable'); END;
CREATE TRIGGER trg_backup_sets_delete_prd100
BEFORE DELETE ON backup_sets
BEGIN SELECT RAISE(ABORT,'backup_set_immutable'); END;

CREATE TABLE backup_restore_verifications(
  backup_restore_verification_id TEXT PRIMARY KEY CHECK(
    length(backup_restore_verification_id)=36
    AND substr(backup_restore_verification_id,1,4)='brv_'),
  backup_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','running','passed','failed')),
  schema_head TEXT CHECK(schema_head IS NULL OR length(schema_head)=4),
  integrity_status TEXT CHECK(integrity_status IS NULL
    OR integrity_status IN('ok','failed')),
  manifest_sha256 TEXT CHECK(manifest_sha256 IS NULL OR length(manifest_sha256)=64),
  tenant_owner_sample_count INTEGER CHECK(tenant_owner_sample_count IS NULL
    OR tenant_owner_sample_count BETWEEN 0 AND 10000),
  error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 3 AND 96),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(backup_id) REFERENCES backup_sets(backup_id),
  CHECK((status='pending' AND started_at IS NULL AND completed_at IS NULL
      AND error_code IS NULL)
    OR(status='running' AND started_at IS NOT NULL AND completed_at IS NULL
      AND error_code IS NULL)
    OR(status='passed' AND started_at IS NOT NULL AND completed_at IS NOT NULL
      AND schema_head IS NOT NULL AND integrity_status='ok'
      AND manifest_sha256 IS NOT NULL AND tenant_owner_sample_count IS NOT NULL
      AND error_code IS NULL)
    OR(status='failed' AND started_at IS NOT NULL AND completed_at IS NOT NULL
      AND error_code IS NOT NULL))
);
CREATE TRIGGER trg_backup_verification_owner_update_prd100
BEFORE UPDATE ON backup_restore_verifications FOR EACH ROW
WHEN NEW.backup_restore_verification_id!=OLD.backup_restore_verification_id
  OR NEW.backup_id!=OLD.backup_id OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'backup_verification_owner_immutable'); END;
CREATE TRIGGER trg_backup_restore_verifications_delete_prd100
BEFORE DELETE ON backup_restore_verifications
BEGIN SELECT RAISE(ABORT,'backup_verification_immutable'); END;

CREATE TABLE infrastructure_event_outbox(
  infrastructure_event_id TEXT PRIMARY KEY CHECK(
    length(infrastructure_event_id)=36
    AND substr(infrastructure_event_id,1,4)='ife_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'infrastructure.degraded','backup.completed','backup.failed','migration.completed')),
  object_type TEXT NOT NULL CHECK(object_type IN(
    'infrastructure_resource','backup_set','migration_lease')),
  object_id TEXT NOT NULL CHECK(length(object_id)=36),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('pending','processing','completed','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  error_code TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK((status='processing' AND lease_owner_id IS NOT NULL
      AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    OR(status!='processing' AND lease_owner_id IS NULL
      AND lease_token IS NULL AND lease_expires_at IS NULL)),
  CHECK((status='failed' AND error_code IS NOT NULL)
    OR(status!='failed' AND error_code IS NULL))
);
CREATE INDEX idx_infrastructure_event_outbox_status_prd100
  ON infrastructure_event_outbox(status,next_attempt_at,infrastructure_event_id);

CREATE TEMP TABLE prd100_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd100_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM infrastructure_resources)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_infrastructure_permission_extensions)=4
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_infrastructure_role_permissions)=9
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd100_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
