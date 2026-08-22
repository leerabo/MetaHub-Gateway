-- 建立云网关 Cell、Workspace 放置、容量租约与可靠配置发布的权威持久边界。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd101_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 前置守卫要求迁移顺序、依赖表、临时命名空间和数据库完整性全部满足。
INSERT INTO prd101_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0092' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_roles','platform_operators','workspaces','workspace_tenant_bindings',
      'workspace_regions','workspace_operational_states'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd101')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd101_up_guard;

CREATE TABLE platform_gateway_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.gateway.read','platform.gateway.manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-101'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=5)
);
CREATE TABLE platform_gateway_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_gateway_permission_extensions(permission_code)
);
-- 权限扩展固定由 PRD-101 版本目录声明，禁止运行期自行扩展管理边界。
INSERT INTO platform_gateway_permission_extensions VALUES
  ('platform.gateway.read','PRD-101',5),
  ('platform.gateway.manage','PRD-101',5);
-- 平台角色只获得其职责所需的查看或管理权限，发布角色保持只读。
INSERT INTO platform_gateway_role_permissions VALUES
  ('platform_owner','platform.gateway.read'),
  ('platform_owner','platform.gateway.manage'),
  ('platform_ops','platform.gateway.read'),
  ('platform_ops','platform.gateway.manage'),
  ('release_manager','platform.gateway.read');
CREATE TRIGGER trg_platform_gateway_permissions_insert_prd101
BEFORE INSERT ON platform_gateway_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_gateway_permission_immutable'); END;
CREATE TRIGGER trg_platform_gateway_permissions_update_prd101
BEFORE UPDATE ON platform_gateway_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_gateway_permission_immutable'); END;
CREATE TRIGGER trg_platform_gateway_permissions_delete_prd101
BEFORE DELETE ON platform_gateway_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_gateway_permission_immutable'); END;
CREATE TRIGGER trg_platform_gateway_role_permissions_insert_prd101
BEFORE INSERT ON platform_gateway_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_gateway_permission_immutable'); END;
CREATE TRIGGER trg_platform_gateway_role_permissions_update_prd101
BEFORE UPDATE ON platform_gateway_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_gateway_permission_immutable'); END;
CREATE TRIGGER trg_platform_gateway_role_permissions_delete_prd101
BEFORE DELETE ON platform_gateway_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_gateway_permission_immutable'); END;

CREATE TABLE gateway_cells(
  gateway_cell_id TEXT PRIMARY KEY CHECK(length(gateway_cell_id)=36
    AND substr(gateway_cell_id,1,4)='gcl_'),
  cell_code TEXT NOT NULL UNIQUE CHECK(length(cell_code) BETWEEN 3 AND 40
    AND cell_code=lower(cell_code) AND cell_code NOT GLOB '*[^a-z0-9-]*'),
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 3 AND 40
    AND region_code=lower(region_code) AND region_code NOT GLOB '*[^a-z0-9-]*'),
  capacity_class TEXT NOT NULL CHECK(capacity_class IN('standard','high','burst')),
  status TEXT NOT NULL CHECK(status IN('active','draining','unavailable')),
  health_status TEXT NOT NULL CHECK(health_status IN('healthy','degraded','unavailable')),
  capacity_limit INTEGER NOT NULL CHECK(capacity_limit BETWEEN 1 AND 1000000),
  admitted_leases INTEGER NOT NULL DEFAULT 0 CHECK(admitted_leases BETWEEN 0 AND capacity_limit),
  release_watermark INTEGER NOT NULL DEFAULT 0 CHECK(release_watermark>=0),
  config_watermark INTEGER NOT NULL DEFAULT 0 CHECK(config_watermark>=0),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
-- 初始 Cell 仅提供健康且未占用的受管容量，后续状态必须通过 revision 更新。
INSERT INTO gateway_cells VALUES
  ('gcl_00000000000000000000000000000001','cn-north-1-a','cn-north-1','standard',
   'active','healthy',500,0,0,0,1,datetime('now'),datetime('now')),
  ('gcl_00000000000000000000000000000002','cn-east-1-a','cn-east-1','standard',
   'active','healthy',500,0,0,0,1,datetime('now'),datetime('now'));
CREATE INDEX idx_gateway_cells_region_prd101
  ON gateway_cells(region_code,status,cell_code);
CREATE TRIGGER trg_gateway_cells_owner_update_prd101
BEFORE UPDATE ON gateway_cells FOR EACH ROW
WHEN NEW.gateway_cell_id!=OLD.gateway_cell_id OR NEW.cell_code!=OLD.cell_code
  OR NEW.region_code!=OLD.region_code OR NEW.capacity_class!=OLD.capacity_class
  OR NEW.capacity_limit!=OLD.capacity_limit OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'gateway_cell_owner_immutable'); END;
CREATE TRIGGER trg_gateway_cells_delete_prd101
BEFORE DELETE ON gateway_cells
BEGIN SELECT RAISE(ABORT,'gateway_cell_immutable'); END;

CREATE TABLE workspace_gateway_placements(
  workspace_gateway_placement_id TEXT PRIMARY KEY CHECK(
    length(workspace_gateway_placement_id)=36
    AND substr(workspace_gateway_placement_id,1,4)='wgp_'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  primary_region_code TEXT NOT NULL,
  primary_gateway_cell_id TEXT NOT NULL,
  failover_policy TEXT NOT NULL CHECK(failover_policy IN('deny','explicit')),
  failover_gateway_cell_id TEXT,
  residency_policy_version TEXT NOT NULL CHECK(
    length(residency_policy_version) BETWEEN 1 AND 80),
  revision INTEGER NOT NULL CHECK(revision>=1),
  snapshot_generation INTEGER NOT NULL CHECK(snapshot_generation>=1),
  updated_by_operator_id TEXT NOT NULL,
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(primary_gateway_cell_id) REFERENCES gateway_cells(gateway_cell_id),
  FOREIGN KEY(failover_gateway_cell_id) REFERENCES gateway_cells(gateway_cell_id),
  FOREIGN KEY(updated_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((failover_policy='deny' AND failover_gateway_cell_id IS NULL)
    OR(failover_policy='explicit' AND failover_gateway_cell_id IS NOT NULL
      AND failover_gateway_cell_id!=primary_gateway_cell_id))
);
CREATE INDEX idx_gateway_placements_primary_prd101
  ON workspace_gateway_placements(primary_gateway_cell_id,workspace_id);
CREATE TRIGGER trg_gateway_placement_owner_update_prd101
BEFORE UPDATE ON workspace_gateway_placements FOR EACH ROW
WHEN NEW.workspace_gateway_placement_id!=OLD.workspace_gateway_placement_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.snapshot_generation!=OLD.snapshot_generation+1
BEGIN SELECT RAISE(ABORT,'gateway_placement_owner_immutable'); END;
CREATE TRIGGER trg_gateway_placements_delete_prd101
BEFORE DELETE ON workspace_gateway_placements
BEGIN SELECT RAISE(ABORT,'gateway_placement_immutable'); END;

CREATE TABLE gateway_capacity_leases(
  gateway_capacity_lease_id TEXT PRIMARY KEY CHECK(
    length(gateway_capacity_lease_id)=36
    AND substr(gateway_capacity_lease_id,1,4)='gcp_'),
  gateway_cell_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  virtual_key_id TEXT NOT NULL,
  resource_class TEXT NOT NULL CHECK(resource_class IN('chat','embeddings')),
  quota_units INTEGER NOT NULL CHECK(quota_units BETWEEN 1 AND 100000),
  owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 8 AND 128),
  lease_token_sha256 TEXT NOT NULL CHECK(length(lease_token_sha256)=64),
  generation INTEGER NOT NULL CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN('active','released','expired')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  acquired_at TEXT NOT NULL CHECK(unixepoch(acquired_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  FOREIGN KEY(gateway_cell_id) REFERENCES gateway_cells(gateway_cell_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(virtual_key_id) REFERENCES virtual_keys(virtual_key_id),
  CHECK((status='active' AND released_at IS NULL)
    OR(status IN('released','expired') AND released_at IS NOT NULL))
);
CREATE INDEX idx_gateway_capacity_active_prd101
  ON gateway_capacity_leases(gateway_cell_id,status,expires_at);
CREATE TRIGGER trg_gateway_capacity_owner_update_prd101
BEFORE UPDATE ON gateway_capacity_leases FOR EACH ROW
WHEN NEW.gateway_capacity_lease_id!=OLD.gateway_capacity_lease_id
  OR NEW.gateway_cell_id!=OLD.gateway_cell_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.virtual_key_id!=OLD.virtual_key_id
  OR NEW.resource_class!=OLD.resource_class OR NEW.quota_units!=OLD.quota_units
  OR NEW.owner_id!=OLD.owner_id OR NEW.lease_token_sha256!=OLD.lease_token_sha256
  OR NEW.generation!=OLD.generation OR NEW.acquired_at!=OLD.acquired_at
BEGIN SELECT RAISE(ABORT,'gateway_capacity_owner_immutable'); END;

CREATE TABLE gateway_config_snapshot_leases(
  gateway_config_snapshot_lease_id TEXT PRIMARY KEY CHECK(
    length(gateway_config_snapshot_lease_id)=36
    AND substr(gateway_config_snapshot_lease_id,1,4)='gcs_'),
  gateway_cell_id TEXT NOT NULL,
  worker_id TEXT NOT NULL CHECK(length(worker_id) BETWEEN 8 AND 128),
  snapshot_id TEXT NOT NULL CHECK(length(snapshot_id) BETWEEN 8 AND 128),
  snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64),
  signing_key_version INTEGER NOT NULL CHECK(signing_key_version>=1),
  config_watermark INTEGER NOT NULL CHECK(config_watermark>=1),
  generation INTEGER NOT NULL CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN('active','released','expired')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  acquired_at TEXT NOT NULL CHECK(unixepoch(acquired_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  FOREIGN KEY(gateway_cell_id) REFERENCES gateway_cells(gateway_cell_id),
  UNIQUE(gateway_cell_id,worker_id,generation)
);

CREATE TABLE gateway_releases(
  gateway_release_id TEXT PRIMARY KEY CHECK(length(gateway_release_id)=36
    AND substr(gateway_release_id,1,4)='grl_'),
  artifact_sha256 TEXT NOT NULL UNIQUE CHECK(length(artifact_sha256)=64),
  schema_min_version TEXT NOT NULL CHECK(length(schema_min_version)=4),
  schema_max_version TEXT NOT NULL CHECK(length(schema_max_version)=4),
  rollout_state TEXT NOT NULL CHECK(rollout_state IN(
    'draft','canary','active','rolled_back','failed')),
  health_gate TEXT NOT NULL CHECK(health_gate IN('pending','passed','failed')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  created_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id)
);
CREATE TRIGGER trg_gateway_release_owner_update_prd101
BEFORE UPDATE ON gateway_releases FOR EACH ROW
WHEN NEW.gateway_release_id!=OLD.gateway_release_id
  OR NEW.artifact_sha256!=OLD.artifact_sha256
  OR NEW.schema_min_version!=OLD.schema_min_version
  OR NEW.schema_max_version!=OLD.schema_max_version
  OR NEW.generation!=OLD.generation
  OR NEW.created_by_operator_id!=OLD.created_by_operator_id
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'gateway_release_owner_immutable'); END;

CREATE TABLE gateway_cell_events(
  gateway_cell_event_id TEXT PRIMARY KEY CHECK(length(gateway_cell_event_id)=36
    AND substr(gateway_cell_event_id,1,4)='gce_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'placement.changed','capacity.exhausted','cell.status_changed','release.changed')),
  gateway_cell_id TEXT NOT NULL,
  workspace_id TEXT,
  tenant_id TEXT,
  object_id TEXT NOT NULL CHECK(length(object_id) BETWEEN 3 AND 128),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(gateway_cell_id) REFERENCES gateway_cells(gateway_cell_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  CHECK((workspace_id IS NULL AND tenant_id IS NULL)
    OR(workspace_id IS NOT NULL AND tenant_id IS NOT NULL))
);
CREATE TRIGGER trg_gateway_cell_events_update_prd101
BEFORE UPDATE ON gateway_cell_events
BEGIN SELECT RAISE(ABORT,'gateway_cell_event_immutable'); END;
CREATE TRIGGER trg_gateway_cell_events_delete_prd101
BEFORE DELETE ON gateway_cell_events
BEGIN SELECT RAISE(ABORT,'gateway_cell_event_immutable'); END;

CREATE TABLE gateway_config_outbox(
  gateway_config_outbox_id TEXT PRIMARY KEY CHECK(length(gateway_config_outbox_id)=36
    AND substr(gateway_config_outbox_id,1,4)='gco_'),
  gateway_cell_event_id TEXT NOT NULL UNIQUE,
  gateway_cell_id TEXT NOT NULL,
  snapshot_generation INTEGER NOT NULL CHECK(snapshot_generation>=1),
  snapshot_id TEXT CHECK(snapshot_id IS NULL OR (length(snapshot_id)=36
    AND substr(snapshot_id,1,4)='gss_')),
  snapshot_payload TEXT CHECK(snapshot_payload IS NULL
    OR length(snapshot_payload) BETWEEN 100 AND 4096),
  snapshot_payload_sha256 TEXT CHECK(snapshot_payload_sha256 IS NULL
    OR length(snapshot_payload_sha256)=64),
  signing_key_version INTEGER CHECK(signing_key_version IS NULL
    OR signing_key_version>=1),
  status TEXT NOT NULL CHECK(status IN('pending','processing','completed','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  published_at TEXT CHECK(published_at IS NULL OR unixepoch(published_at) IS NOT NULL),
  expires_at TEXT CHECK(expires_at IS NULL OR unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(gateway_cell_event_id) REFERENCES gateway_cell_events(gateway_cell_event_id),
  FOREIGN KEY(gateway_cell_id) REFERENCES gateway_cells(gateway_cell_id),
  CHECK((status!='completed' AND snapshot_id IS NULL AND snapshot_payload IS NULL
      AND snapshot_payload_sha256 IS NULL AND signing_key_version IS NULL
      AND published_at IS NULL AND expires_at IS NULL)
    OR(status='completed' AND snapshot_id IS NOT NULL AND snapshot_payload IS NOT NULL
      AND snapshot_payload_sha256 IS NOT NULL AND signing_key_version IS NOT NULL
      AND published_at IS NOT NULL AND expires_at IS NOT NULL
      AND unixepoch(expires_at)>unixepoch(published_at)))
);
CREATE INDEX idx_gateway_config_outbox_claim_prd101
  ON gateway_config_outbox(status,next_attempt_at,gateway_config_outbox_id);
CREATE TRIGGER trg_gateway_cell_event_outbox_prd101
AFTER INSERT ON gateway_cell_events FOR EACH ROW
WHEN NEW.event_type='placement.changed'
BEGIN
  -- placement 事件与待发布任务在同一事务落库，避免提交后丢失配置通知。
  INSERT INTO gateway_config_outbox(
    gateway_config_outbox_id,gateway_cell_event_id,gateway_cell_id,
    snapshot_generation,status,attempt_count,
    next_attempt_at,created_at,updated_at)
  VALUES('gco_'||lower(hex(randomblob(16))),NEW.gateway_cell_event_id,
    NEW.gateway_cell_id,1,'pending',0,
    NEW.occurred_at,NEW.occurred_at,NEW.occurred_at);
END;

CREATE TEMP TABLE prd101_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 最终守卫确认固定种子数量、外键和数据库完整性，异常时回滚整个迁移。
INSERT INTO prd101_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM gateway_cells)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_gateway_permission_extensions)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_gateway_role_permissions)=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd101_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
