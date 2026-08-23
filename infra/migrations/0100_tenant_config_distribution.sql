-- Versioned tenant configuration distribution, monotonic ACK watermarks, and rebuild jobs.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd108_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd108_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0099' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_roles','platform_operators','platform_job_types','platform_jobs',
      'workspaces','workspace_tenant_bindings','workspace_operational_states',
      'workspace_gateway_placements','gateway_cells','gateway_config_snapshot_leases'))=10
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd108')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd108_up_guard;

CREATE TABLE platform_config_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.config_distribution.read','platform.config_distribution.rebuild')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-108'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=10)
);
CREATE TABLE platform_config_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_config_permission_extensions(permission_code)
);
INSERT INTO platform_config_permission_extensions VALUES
  ('platform.config_distribution.read','PRD-108',10),
  ('platform.config_distribution.rebuild','PRD-108',10);
INSERT INTO platform_config_role_permissions VALUES
  ('platform_owner','platform.config_distribution.read'),
  ('platform_owner','platform.config_distribution.rebuild'),
  ('platform_ops','platform.config_distribution.read'),
  ('platform_ops','platform.config_distribution.rebuild'),
  ('platform_security','platform.config_distribution.read');
CREATE TRIGGER trg_platform_config_permissions_insert_prd108
BEFORE INSERT ON platform_config_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_config_permission_immutable'); END;
CREATE TRIGGER trg_platform_config_permissions_update_prd108
BEFORE UPDATE ON platform_config_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_config_permission_immutable'); END;
CREATE TRIGGER trg_platform_config_permissions_delete_prd108
BEFORE DELETE ON platform_config_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_config_permission_immutable'); END;
CREATE TRIGGER trg_platform_config_role_permissions_insert_prd108
BEFORE INSERT ON platform_config_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_config_permission_immutable'); END;
CREATE TRIGGER trg_platform_config_role_permissions_update_prd108
BEFORE UPDATE ON platform_config_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_config_permission_immutable'); END;
CREATE TRIGGER trg_platform_config_role_permissions_delete_prd108
BEFORE DELETE ON platform_config_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_config_permission_immutable'); END;

CREATE TABLE config_snapshots(
  config_snapshot_id TEXT PRIMARY KEY CHECK(length(config_snapshot_id)=36
    AND substr(config_snapshot_id,1,4)='csn_'
    AND substr(config_snapshot_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  manifest_envelope TEXT NOT NULL CHECK(length(CAST(manifest_envelope AS BLOB)) BETWEEN 64 AND 262144),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64
    AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
  kms_key_version TEXT NOT NULL CHECK(length(kms_key_version) BETWEEN 3 AND 96),
  signature_algorithm TEXT NOT NULL CHECK(signature_algorithm IN(
    'ed25519','rsassa_pss_sha256')),
  signature_b64 TEXT NOT NULL CHECK(length(signature_b64) BETWEEN 43 AND 2048),
  revocation_watermark INTEGER NOT NULL CHECK(revocation_watermark>=0),
  published_at TEXT NOT NULL CHECK(unixepoch(published_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,generation),
  UNIQUE(workspace_id,tenant_id,generation,config_snapshot_id)
);
CREATE INDEX idx_config_snapshots_owner_prd108
  ON config_snapshots(workspace_id,generation DESC);
CREATE TRIGGER trg_config_snapshots_update_prd108 BEFORE UPDATE ON config_snapshots
BEGIN SELECT RAISE(ABORT,'config_snapshot_immutable'); END;
CREATE TRIGGER trg_config_snapshots_delete_prd108 BEFORE DELETE ON config_snapshots
BEGIN SELECT RAISE(ABORT,'config_snapshot_immutable'); END;

CREATE TABLE config_snapshot_heads(
  config_snapshot_head_id TEXT PRIMARY KEY CHECK(length(config_snapshot_head_id)=36
    AND substr(config_snapshot_head_id,1,4)='csh_'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL CHECK(generation>=1),
  config_snapshot_id TEXT NOT NULL UNIQUE,
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  revocation_watermark INTEGER NOT NULL CHECK(revocation_watermark>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  published_at TEXT NOT NULL CHECK(unixepoch(published_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,generation,config_snapshot_id)
    REFERENCES config_snapshots(workspace_id,tenant_id,generation,config_snapshot_id)
);
CREATE TRIGGER trg_config_snapshot_heads_owner_update_prd108
BEFORE UPDATE ON config_snapshot_heads FOR EACH ROW
WHEN NEW.config_snapshot_head_id!=OLD.config_snapshot_head_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.generation!=OLD.generation+1 OR NEW.revision!=OLD.revision+1
  OR NEW.revocation_watermark<OLD.revocation_watermark
  OR NEW.published_at<=OLD.published_at OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'config_snapshot_head_not_monotonic'); END;
CREATE TRIGGER trg_config_snapshot_heads_delete_prd108 BEFORE DELETE ON config_snapshot_heads
BEGIN SELECT RAISE(ABORT,'config_snapshot_head_immutable'); END;

CREATE TABLE config_deltas(
  config_delta_id TEXT PRIMARY KEY CHECK(length(config_delta_id)=36
    AND substr(config_delta_id,1,4)='cdl_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  from_generation INTEGER NOT NULL CHECK(from_generation>=1),
  to_generation INTEGER NOT NULL CHECK(to_generation>from_generation),
  from_manifest_sha256 TEXT NOT NULL CHECK(length(from_manifest_sha256)=64),
  to_manifest_sha256 TEXT NOT NULL CHECK(length(to_manifest_sha256)=64),
  categories_json TEXT NOT NULL CHECK(json_valid(categories_json)
    AND json_type(categories_json)='array' AND length(CAST(categories_json AS BLOB))<=1024),
  patch_envelope TEXT NOT NULL CHECK(length(CAST(patch_envelope AS BLOB)) BETWEEN 64 AND 262144),
  patch_sha256 TEXT NOT NULL CHECK(length(patch_sha256)=64),
  full_rebuild INTEGER NOT NULL CHECK(full_rebuild IN(0,1)),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,from_generation,to_generation),
  CHECK(full_rebuild=1 OR to_generation=from_generation+1)
);
CREATE INDEX idx_config_deltas_target_prd108
  ON config_deltas(workspace_id,to_generation DESC);
CREATE TRIGGER trg_config_deltas_update_prd108 BEFORE UPDATE ON config_deltas
BEGIN SELECT RAISE(ABORT,'config_delta_immutable'); END;
CREATE TRIGGER trg_config_deltas_delete_prd108 BEFORE DELETE ON config_deltas
BEGIN SELECT RAISE(ABORT,'config_delta_immutable'); END;

CREATE TABLE config_acks(
  config_ack_id TEXT PRIMARY KEY CHECK(length(config_ack_id)=36
    AND substr(config_ack_id,1,4)='cak_'),
  config_snapshot_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  gateway_cell_id TEXT NOT NULL,
  gateway_identity_sha256 TEXT NOT NULL CHECK(length(gateway_identity_sha256)=64),
  generation INTEGER NOT NULL CHECK(generation>=1),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  ack_status TEXT NOT NULL CHECK(ack_status IN('applied','failed')),
  disposition TEXT NOT NULL CHECK(disposition IN('accepted','late')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'applied','apply_failed','superseded_generation')),
  acked_at TEXT NOT NULL CHECK(unixepoch(acked_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,generation,config_snapshot_id)
    REFERENCES config_snapshots(workspace_id,tenant_id,generation,config_snapshot_id),
  FOREIGN KEY(gateway_cell_id) REFERENCES gateway_cells(gateway_cell_id),
  UNIQUE(gateway_identity_sha256,config_snapshot_id)
);
CREATE INDEX idx_config_acks_lag_prd108
  ON config_acks(workspace_id,generation DESC,acked_at DESC);
CREATE TRIGGER trg_config_acks_update_prd108 BEFORE UPDATE ON config_acks
BEGIN SELECT RAISE(ABORT,'config_ack_immutable'); END;
CREATE TRIGGER trg_config_acks_delete_prd108 BEFORE DELETE ON config_acks
BEGIN SELECT RAISE(ABORT,'config_ack_immutable'); END;

CREATE TABLE cache_watermarks(
  cache_watermark_id TEXT PRIMARY KEY CHECK(length(cache_watermark_id)=36
    AND substr(cache_watermark_id,1,4)='cwm_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  gateway_cell_id TEXT NOT NULL,
  gateway_identity_sha256 TEXT NOT NULL CHECK(length(gateway_identity_sha256)=64),
  observed_generation INTEGER NOT NULL CHECK(observed_generation>=0),
  applied_generation INTEGER NOT NULL CHECK(applied_generation>=0),
  lkg_generation INTEGER NOT NULL CHECK(lkg_generation>=0),
  observed_revocation_watermark INTEGER NOT NULL CHECK(observed_revocation_watermark>=0),
  applied_revocation_watermark INTEGER NOT NULL CHECK(applied_revocation_watermark>=0),
  stale_reason TEXT NOT NULL CHECK(stale_reason IN(
    'none','unreachable','revocation_lag','signature_invalid','owner_invalid','hash_invalid')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  last_observed_at TEXT NOT NULL CHECK(unixepoch(last_observed_at) IS NOT NULL),
  last_applied_at TEXT CHECK(last_applied_at IS NULL OR unixepoch(last_applied_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(gateway_cell_id) REFERENCES gateway_cells(gateway_cell_id),
  UNIQUE(workspace_id,gateway_identity_sha256),
  CHECK(applied_generation<=observed_generation AND lkg_generation<=applied_generation
    AND applied_revocation_watermark<=observed_revocation_watermark)
);
CREATE INDEX idx_cache_watermarks_lag_prd108
  ON cache_watermarks(stale_reason,observed_generation,applied_generation,workspace_id);
CREATE TRIGGER trg_cache_watermarks_owner_update_prd108
BEFORE UPDATE ON cache_watermarks FOR EACH ROW
WHEN NEW.cache_watermark_id!=OLD.cache_watermark_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.gateway_cell_id!=OLD.gateway_cell_id
  OR NEW.gateway_identity_sha256!=OLD.gateway_identity_sha256
  OR NEW.observed_generation<OLD.observed_generation
  OR NEW.applied_generation<OLD.applied_generation
  OR NEW.lkg_generation<OLD.lkg_generation
  OR NEW.observed_revocation_watermark<OLD.observed_revocation_watermark
  OR NEW.applied_revocation_watermark<OLD.applied_revocation_watermark
  OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'cache_watermark_not_monotonic'); END;
CREATE TRIGGER trg_cache_watermarks_delete_prd108 BEFORE DELETE ON cache_watermarks
BEGIN SELECT RAISE(ABORT,'cache_watermark_immutable'); END;

CREATE TABLE config_rebuild_jobs(
  config_rebuild_job_id TEXT PRIMARY KEY CHECK(length(config_rebuild_job_id)=36
    AND substr(config_rebuild_job_id,1,4)='crj_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  target_generation INTEGER NOT NULL CHECK(target_generation>=1),
  platform_job_id TEXT NOT NULL UNIQUE,
  requested_by_operator_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('queued','running','completed','failed')),
  result_manifest_sha256 TEXT CHECK(result_manifest_sha256 IS NULL
    OR length(result_manifest_sha256)=64),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  FOREIGN KEY(requested_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(requested_by_operator_id,idempotency_sha256),
  CHECK((status IN('completed','failed') AND completed_at IS NOT NULL)
    OR(status IN('queued','running') AND completed_at IS NULL)),
  CHECK((status='completed' AND result_manifest_sha256 IS NOT NULL)
    OR(status!='completed' AND result_manifest_sha256 IS NULL))
);
CREATE INDEX idx_config_rebuild_jobs_owner_prd108
  ON config_rebuild_jobs(workspace_id,created_at DESC);
CREATE TRIGGER trg_config_rebuild_jobs_owner_update_prd108
BEFORE UPDATE ON config_rebuild_jobs FOR EACH ROW
WHEN NEW.config_rebuild_job_id!=OLD.config_rebuild_job_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.target_generation!=OLD.target_generation
  OR NEW.platform_job_id!=OLD.platform_job_id
  OR NEW.requested_by_operator_id!=OLD.requested_by_operator_id
  OR NEW.idempotency_sha256!=OLD.idempotency_sha256
  OR NEW.request_sha256!=OLD.request_sha256
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'config_rebuild_owner_immutable'); END;
CREATE TRIGGER trg_config_rebuild_jobs_delete_prd108 BEFORE DELETE ON config_rebuild_jobs
BEGIN SELECT RAISE(ABORT,'config_rebuild_job_immutable'); END;

CREATE TABLE config_event_outbox(
  config_event_outbox_id TEXT PRIMARY KEY CHECK(length(config_event_outbox_id)=36
    AND substr(config_event_outbox_id,1,4)='ceo_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'config.snapshot_published','config.ack_lagging','config.rebuild_completed')),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  gateway_cell_id TEXT,
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'snapshot_published','ack_lagging','rebuild_completed')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(gateway_cell_id) REFERENCES gateway_cells(gateway_cell_id)
);
CREATE TRIGGER trg_config_event_outbox_update_prd108 BEFORE UPDATE ON config_event_outbox
BEGIN SELECT RAISE(ABORT,'config_event_immutable'); END;
CREATE TRIGGER trg_config_event_outbox_delete_prd108 BEFORE DELETE ON config_event_outbox
BEGIN SELECT RAISE(ABORT,'config_event_immutable'); END;

UPDATE platform_job_types
SET status='active',revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE type_code='config.snapshot.rebuild' AND status='disabled' AND revision=1;

CREATE TEMP TABLE prd108_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd108_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_config_permission_extensions)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_config_role_permissions)=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='config.snapshot.rebuild')='active:2' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'config_snapshots','config_snapshot_heads','config_deltas','config_acks',
      'cache_watermarks','config_rebuild_jobs','config_event_outbox'))=7 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd108_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
