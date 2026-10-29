-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd117_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd117_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0108' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_tenant_bindings','workspace_regions','workspace_gateway_placements',
      'backup_sets','platform_regions','platform_job_types','config_snapshot_heads'))=7
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd117')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd117_up_guard;

CREATE TABLE workspace_governance_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'data_governance.read','data_governance.manage','legal_hold.read','legal_hold.manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-117'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=28)
);
CREATE TABLE workspace_governance_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code) REFERENCES workspace_governance_permission_extensions(permission_code)
);
INSERT INTO workspace_governance_permission_extensions VALUES
  ('data_governance.read','PRD-117',28),
  ('data_governance.manage','PRD-117',28),
  ('legal_hold.read','PRD-117',28),
  ('legal_hold.manage','PRD-117',28);
INSERT INTO workspace_governance_role_permissions VALUES
  ('owner','data_governance.read'),('owner','data_governance.manage'),
  ('owner','legal_hold.read'),('owner','legal_hold.manage'),
  ('security_admin','data_governance.read'),('security_admin','data_governance.manage'),
  ('security_admin','legal_hold.read'),
  ('legal_admin','data_governance.read'),('legal_admin','legal_hold.read'),
  ('legal_admin','legal_hold.manage'),
  ('auditor','data_governance.read'),('auditor','legal_hold.read');
CREATE TRIGGER trg_workspace_governance_permissions_insert_prd117
BEFORE INSERT ON workspace_governance_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_governance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_governance_permissions_update_prd117
BEFORE UPDATE ON workspace_governance_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_governance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_governance_permissions_delete_prd117
BEFORE DELETE ON workspace_governance_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_governance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_governance_roles_insert_prd117
BEFORE INSERT ON workspace_governance_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_governance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_governance_roles_update_prd117
BEFORE UPDATE ON workspace_governance_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_governance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_governance_roles_delete_prd117
BEFORE DELETE ON workspace_governance_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_governance_permission_immutable'); END;

CREATE TABLE platform_data_governance_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code='platform.data_residency.read'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-117'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=16)
);
CREATE TABLE platform_data_governance_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_data_governance_permission_extensions(permission_code)
);
INSERT INTO platform_data_governance_permission_extensions VALUES
  ('platform.data_residency.read','PRD-117',16);
INSERT INTO platform_data_governance_role_permissions VALUES
  ('platform_owner','platform.data_residency.read'),
  ('platform_compliance','platform.data_residency.read');
CREATE TRIGGER trg_platform_data_governance_permissions_insert_prd117
BEFORE INSERT ON platform_data_governance_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_data_governance_permission_immutable'); END;
CREATE TRIGGER trg_platform_data_governance_permissions_update_prd117
BEFORE UPDATE ON platform_data_governance_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_data_governance_permission_immutable'); END;
CREATE TRIGGER trg_platform_data_governance_permissions_delete_prd117
BEFORE DELETE ON platform_data_governance_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_data_governance_permission_immutable'); END;
CREATE TRIGGER trg_platform_data_governance_roles_insert_prd117
BEFORE INSERT ON platform_data_governance_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_data_governance_permission_immutable'); END;
CREATE TRIGGER trg_platform_data_governance_roles_update_prd117
BEFORE UPDATE ON platform_data_governance_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_data_governance_permission_immutable'); END;
CREATE TRIGGER trg_platform_data_governance_roles_delete_prd117
BEFORE DELETE ON platform_data_governance_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_data_governance_permission_immutable'); END;

CREATE TABLE data_classifications(
  data_classification_id TEXT PRIMARY KEY CHECK(length(data_classification_id)=36
    AND substr(data_classification_id,1,4)='dcl_'
    AND substr(data_classification_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL CHECK(code IN('prompt','response','file','audit','identity','billing')),
  sensitivity_level TEXT NOT NULL CHECK(sensitivity_level IN('restricted','confidential','internal')),
  allowed_purposes_json TEXT NOT NULL CHECK(json_valid(allowed_purposes_json)
    AND json_type(allowed_purposes_json)='array' AND length(allowed_purposes_json)<=512),
  default_retention_days INTEGER NOT NULL CHECK(default_retention_days BETWEEN 1 AND 3650),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,code),
  UNIQUE(workspace_id,tenant_id,data_classification_id)
);
CREATE TRIGGER trg_data_classifications_update_prd117 BEFORE UPDATE ON data_classifications
BEGIN SELECT RAISE(ABORT,'data_classification_immutable'); END;
CREATE TRIGGER trg_data_classifications_delete_prd117 BEFORE DELETE ON data_classifications
BEGIN SELECT RAISE(ABORT,'data_classification_immutable'); END;

CREATE TABLE data_governance_fences(
  data_governance_fence_id TEXT PRIMARY KEY CHECK(length(data_governance_fence_id)=36
    AND substr(data_governance_fence_id,1,4)='dgf_'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  policy_generation INTEGER NOT NULL CHECK(policy_generation>=1),
  hold_generation INTEGER NOT NULL CHECK(hold_generation>=0),
  deletion_generation INTEGER NOT NULL CHECK(deletion_generation>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,tenant_id,policy_generation,hold_generation)
);
CREATE TRIGGER trg_data_governance_fences_transition_prd117
BEFORE UPDATE ON data_governance_fences FOR EACH ROW
WHEN NEW.data_governance_fence_id!=OLD.data_governance_fence_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR NEW.policy_generation<OLD.policy_generation OR NEW.policy_generation>OLD.policy_generation+1
  OR NEW.hold_generation<OLD.hold_generation OR NEW.hold_generation>OLD.hold_generation+1
  OR NEW.deletion_generation<OLD.deletion_generation OR NEW.deletion_generation>OLD.deletion_generation+1
  OR ((NEW.policy_generation-OLD.policy_generation)
      +(NEW.hold_generation-OLD.hold_generation)
      +(NEW.deletion_generation-OLD.deletion_generation))!=1
BEGIN SELECT RAISE(ABORT,'data_governance_fence_transition_invalid'); END;
CREATE TRIGGER trg_data_governance_fences_delete_prd117 BEFORE DELETE ON data_governance_fences
BEGIN SELECT RAISE(ABORT,'data_governance_fence_immutable'); END;

CREATE TABLE residency_policies(
  residency_policy_id TEXT PRIMARY KEY CHECK(length(residency_policy_id)=36
    AND substr(residency_policy_id,1,4)='rgp_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  allowed_residency_groups_json TEXT NOT NULL CHECK(json_valid(allowed_residency_groups_json)
    AND json_type(allowed_residency_groups_json)='array'
    AND json_array_length(allowed_residency_groups_json) BETWEEN 1 AND 16
    AND length(allowed_residency_groups_json)<=1024),
  primary_residency_group TEXT NOT NULL CHECK(length(primary_residency_group) BETWEEN 3 AND 40
    AND primary_residency_group=lower(primary_residency_group)
    AND primary_residency_group NOT GLOB '*[^a-z0-9-]*'),
  cross_region_mode TEXT NOT NULL CHECK(cross_region_mode IN('deny','same_group')),
  status TEXT NOT NULL CHECK(status IN('active','superseded')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  generation INTEGER NOT NULL CHECK(generation>=1),
  policy_sha256 TEXT NOT NULL CHECK(length(policy_sha256)=64
    AND policy_sha256 NOT GLOB '*[^0-9a-f]*'),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'initial_policy','compliance_update','regional_expansion','regional_restriction','retention_update')),
  updated_by_account_id TEXT,
  valid_until TEXT NOT NULL CHECK(unixepoch(valid_until) IS NOT NULL),
  superseded_at TEXT CHECK(superseded_at IS NULL OR unixepoch(superseded_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(updated_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,generation),
  UNIQUE(workspace_id,tenant_id,residency_policy_id),
  CHECK((status='active' AND superseded_at IS NULL)
    OR(status='superseded' AND superseded_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_residency_policy_active_prd117
  ON residency_policies(workspace_id) WHERE status='active';
CREATE INDEX idx_residency_policy_owner_prd117
  ON residency_policies(workspace_id,tenant_id,generation,status);
CREATE TRIGGER trg_residency_policies_transition_prd117
BEFORE UPDATE ON residency_policies FOR EACH ROW
WHEN NEW.residency_policy_id!=OLD.residency_policy_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.allowed_residency_groups_json!=OLD.allowed_residency_groups_json
  OR NEW.primary_residency_group!=OLD.primary_residency_group
  OR NEW.cross_region_mode!=OLD.cross_region_mode OR NEW.revision!=OLD.revision
  OR NEW.generation!=OLD.generation OR NEW.policy_sha256!=OLD.policy_sha256
  OR NEW.reason_code!=OLD.reason_code
  OR NEW.updated_by_account_id IS NOT OLD.updated_by_account_id
  OR NEW.valid_until!=OLD.valid_until OR NEW.created_at!=OLD.created_at
  OR OLD.status!='active' OR NEW.status!='superseded'
  OR NEW.superseded_at IS NULL OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'residency_policy_transition_invalid'); END;
CREATE TRIGGER trg_residency_policies_delete_prd117 BEFORE DELETE ON residency_policies
BEGIN SELECT RAISE(ABORT,'residency_policy_immutable'); END;

CREATE TABLE retention_policies(
  retention_policy_id TEXT PRIMARY KEY CHECK(length(retention_policy_id)=36
    AND substr(retention_policy_id,1,4)='rtp_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  data_classification_id TEXT NOT NULL,
  classification_code TEXT NOT NULL CHECK(classification_code IN(
    'prompt','response','file','audit','identity','billing')),
  retention_days INTEGER NOT NULL CHECK(retention_days BETWEEN 1 AND 3650),
  deletion_mode TEXT NOT NULL CHECK(deletion_mode IN(
    'hard_delete','archive_then_delete','anonymize')),
  backup_grace_days INTEGER NOT NULL CHECK(backup_grace_days BETWEEN 0 AND 365),
  status TEXT NOT NULL CHECK(status IN('active','superseded')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  generation INTEGER NOT NULL CHECK(generation>=1),
  superseded_at TEXT CHECK(superseded_at IS NULL OR unixepoch(superseded_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,data_classification_id)
    REFERENCES data_classifications(workspace_id,tenant_id,data_classification_id),
  UNIQUE(workspace_id,generation,classification_code),
  UNIQUE(workspace_id,tenant_id,retention_policy_id),
  CHECK((status='active' AND superseded_at IS NULL)
    OR(status='superseded' AND superseded_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_retention_policy_active_prd117
  ON retention_policies(workspace_id,classification_code) WHERE status='active';
CREATE INDEX idx_retention_policy_due_prd117
  ON retention_policies(workspace_id,tenant_id,status,retention_days,classification_code);
CREATE TRIGGER trg_retention_policies_transition_prd117
BEFORE UPDATE ON retention_policies FOR EACH ROW
WHEN NEW.retention_policy_id!=OLD.retention_policy_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.data_classification_id!=OLD.data_classification_id
  OR NEW.classification_code!=OLD.classification_code
  OR NEW.retention_days!=OLD.retention_days OR NEW.deletion_mode!=OLD.deletion_mode
  OR NEW.backup_grace_days!=OLD.backup_grace_days OR NEW.revision!=OLD.revision
  OR NEW.generation!=OLD.generation OR NEW.created_at!=OLD.created_at
  OR OLD.status!='active' OR NEW.status!='superseded'
  OR NEW.superseded_at IS NULL OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'retention_policy_transition_invalid'); END;
CREATE TRIGGER trg_retention_policies_delete_prd117 BEFORE DELETE ON retention_policies
BEGIN SELECT RAISE(ABORT,'retention_policy_immutable'); END;

CREATE TABLE legal_holds(
  legal_hold_id TEXT PRIMARY KEY CHECK(length(legal_hold_id)=36
    AND substr(legal_hold_id,1,4)='lgh_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  data_classification_id TEXT NOT NULL,
  classification_code TEXT NOT NULL CHECK(classification_code IN(
    'prompt','response','file','audit','identity','billing')),
  scope_kind TEXT NOT NULL CHECK(scope_kind IN('classification','object_digest')),
  selector_sha256 TEXT CHECK(selector_sha256 IS NULL OR(length(selector_sha256)=64
    AND selector_sha256 NOT GLOB '*[^0-9a-f]*')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'litigation','regulatory_inquiry','internal_investigation','preservation_notice')),
  status TEXT NOT NULL CHECK(status IN('active','released','expired')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_hold_generation INTEGER NOT NULL CHECK(created_hold_generation>=1),
  released_hold_generation INTEGER CHECK(released_hold_generation IS NULL
    OR released_hold_generation>created_hold_generation),
  legal_actor_account_id TEXT NOT NULL,
  release_reason_code TEXT CHECK(release_reason_code IS NULL OR release_reason_code IN(
    'legal_instruction','matter_closed','hold_expired')),
  expires_at TEXT CHECK(expires_at IS NULL OR unixepoch(expires_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,data_classification_id)
    REFERENCES data_classifications(workspace_id,tenant_id,data_classification_id),
  FOREIGN KEY(legal_actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,legal_hold_id),
  CHECK((scope_kind='classification' AND selector_sha256 IS NULL)
    OR(scope_kind='object_digest' AND selector_sha256 IS NOT NULL)),
  CHECK((status='active' AND released_at IS NULL AND release_reason_code IS NULL
      AND released_hold_generation IS NULL)
    OR(status IN('released','expired') AND released_at IS NOT NULL
      AND release_reason_code IS NOT NULL AND released_hold_generation IS NOT NULL))
);
CREATE UNIQUE INDEX uq_legal_hold_active_prd117
  ON legal_holds(workspace_id,classification_code,scope_kind,coalesce(selector_sha256,''))
  WHERE status='active';
CREATE INDEX idx_legal_hold_predicate_prd117
  ON legal_holds(workspace_id,tenant_id,status,classification_code,expires_at);
CREATE TRIGGER trg_legal_holds_transition_prd117
BEFORE UPDATE ON legal_holds FOR EACH ROW
WHEN NEW.legal_hold_id!=OLD.legal_hold_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.data_classification_id!=OLD.data_classification_id
  OR NEW.classification_code!=OLD.classification_code OR NEW.scope_kind!=OLD.scope_kind
  OR NEW.selector_sha256 IS NOT OLD.selector_sha256 OR NEW.reason_code!=OLD.reason_code
  OR NEW.created_hold_generation!=OLD.created_hold_generation
  OR NEW.legal_actor_account_id!=OLD.legal_actor_account_id
  OR NEW.expires_at IS NOT OLD.expires_at OR NEW.created_at!=OLD.created_at
  OR OLD.status!='active' OR NEW.status NOT IN('released','expired')
  OR NEW.revision!=OLD.revision+1 OR NEW.released_hold_generation IS NULL
  OR NEW.released_hold_generation<=OLD.created_hold_generation
  OR NEW.release_reason_code IS NULL OR NEW.released_at IS NULL OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'legal_hold_transition_invalid'); END;
CREATE TRIGGER trg_legal_holds_delete_prd117 BEFORE DELETE ON legal_holds
BEGIN SELECT RAISE(ABORT,'legal_hold_immutable'); END;

CREATE TABLE deletion_schedules(
  deletion_schedule_id TEXT PRIMARY KEY CHECK(length(deletion_schedule_id)=36
    AND substr(deletion_schedule_id,1,4)='dgs_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  retention_policy_id TEXT NOT NULL,
  classification_code TEXT NOT NULL CHECK(classification_code IN(
    'prompt','response','file','audit','identity','billing')),
  due_at TEXT NOT NULL CHECK(unixepoch(due_at) IS NOT NULL),
  status TEXT NOT NULL CHECK(status IN('queued','running','blocked','completed','failed')),
  policy_generation INTEGER NOT NULL CHECK(policy_generation>=1),
  hold_generation INTEGER NOT NULL CHECK(hold_generation>=0),
  batch_size INTEGER NOT NULL CHECK(batch_size BETWEEN 1 AND 10000),
  checkpoint_code TEXT NOT NULL CHECK(length(checkpoint_code) BETWEEN 1 AND 80
    AND checkpoint_code=lower(checkpoint_code)
    AND checkpoint_code NOT GLOB '*[^a-z0-9._-]*'),
  deleted_count INTEGER NOT NULL CHECK(deleted_count>=0),
  retained_count INTEGER NOT NULL CHECK(retained_count>=0),
  manifest_sha256 TEXT CHECK(manifest_sha256 IS NULL OR(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*')),
  safe_outcome_code TEXT CHECK(safe_outcome_code IS NULL OR(length(safe_outcome_code) BETWEEN 3 AND 96
    AND safe_outcome_code=lower(safe_outcome_code)
    AND safe_outcome_code NOT GLOB '*[^a-z0-9._-]*')),
  platform_job_id TEXT,
  revision INTEGER NOT NULL CHECK(revision>=1),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,retention_policy_id)
    REFERENCES retention_policies(workspace_id,tenant_id,retention_policy_id),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  UNIQUE(workspace_id,tenant_id,deletion_schedule_id),
  UNIQUE(workspace_id,retention_policy_id,due_at),
  CHECK((status='queued' AND started_at IS NULL AND completed_at IS NULL)
    OR(status='running' AND started_at IS NOT NULL AND completed_at IS NULL)
    OR(status IN('blocked','failed') AND completed_at IS NULL)
    OR(status='completed' AND started_at IS NOT NULL AND completed_at IS NOT NULL
      AND manifest_sha256 IS NOT NULL))
);
CREATE INDEX idx_deletion_schedule_due_prd117
  ON deletion_schedules(status,due_at,workspace_id,deletion_schedule_id);
CREATE TRIGGER trg_deletion_schedules_transition_prd117
BEFORE UPDATE ON deletion_schedules FOR EACH ROW
WHEN NEW.deletion_schedule_id!=OLD.deletion_schedule_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.retention_policy_id!=OLD.retention_policy_id
  OR NEW.classification_code!=OLD.classification_code OR NEW.due_at!=OLD.due_at
  OR NEW.policy_generation!=OLD.policy_generation OR NEW.hold_generation<OLD.hold_generation
  OR NEW.batch_size!=OLD.batch_size OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'deletion_schedule_transition_invalid'); END;
CREATE TRIGGER trg_deletion_schedules_delete_prd117 BEFORE DELETE ON deletion_schedules
BEGIN SELECT RAISE(ABORT,'deletion_schedule_immutable'); END;

CREATE TABLE residency_evidence(
  residency_evidence_id TEXT PRIMARY KEY CHECK(length(residency_evidence_id)=36
    AND substr(residency_evidence_id,1,4)='rse_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  residency_policy_id TEXT NOT NULL,
  policy_generation INTEGER NOT NULL CHECK(policy_generation>=1),
  component_code TEXT NOT NULL CHECK(component_code IN(
    'gateway','database','object_storage','backup','job_runtime','disaster_recovery','tenant_data')),
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 3 AND 40
    AND region_code=lower(region_code) AND region_code NOT GLOB '*[^a-z0-9-]*'),
  storage_class TEXT NOT NULL CHECK(storage_class IN(
    'routing','database','object','backup','job','restore','deletion')),
  outcome TEXT NOT NULL CHECK(outcome IN('compliant','migration_required','blocked','violation')),
  violation_code TEXT CHECK(violation_code IS NULL OR violation_code IN(
    'region_not_allowed','generation_stale','policy_expired','policy_unavailable','hold_active')),
  observed_watermark INTEGER NOT NULL CHECK(observed_watermark>=0),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,residency_policy_id)
    REFERENCES residency_policies(workspace_id,tenant_id,residency_policy_id),
  UNIQUE(workspace_id,component_code,observed_watermark,manifest_sha256)
);
CREATE INDEX idx_residency_evidence_platform_prd117
  ON residency_evidence(observed_at,residency_evidence_id,component_code,outcome);
CREATE TRIGGER trg_residency_evidence_update_prd117 BEFORE UPDATE ON residency_evidence
BEGIN SELECT RAISE(ABORT,'residency_evidence_immutable'); END;
CREATE TRIGGER trg_residency_evidence_delete_prd117 BEFORE DELETE ON residency_evidence
BEGIN SELECT RAISE(ABORT,'residency_evidence_immutable'); END;

CREATE TABLE data_governance_commands(
  data_governance_command_id TEXT PRIMARY KEY CHECK(length(data_governance_command_id)=36
    AND substr(data_governance_command_id,1,4)='dgc_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'policy.apply','legal_hold.create','legal_hold.release')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id) BETWEEN 4 AND 80),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  result_generation INTEGER NOT NULL CHECK(result_generation>=0),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,action_code,idempotency_sha256)
);
CREATE TRIGGER trg_data_governance_commands_update_prd117 BEFORE UPDATE ON data_governance_commands
BEGIN SELECT RAISE(ABORT,'data_governance_command_immutable'); END;
CREATE TRIGGER trg_data_governance_commands_delete_prd117 BEFORE DELETE ON data_governance_commands
BEGIN SELECT RAISE(ABORT,'data_governance_command_immutable'); END;

CREATE TABLE data_governance_events(
  data_governance_event_id TEXT PRIMARY KEY CHECK(length(data_governance_event_id)=36
    AND substr(data_governance_event_id,1,4)='dge_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'data.retention_changed','legal_hold.created','deletion.executed','residency.violation_detected')),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  object_type TEXT NOT NULL CHECK(object_type IN(
    'residency_policy','legal_hold','deletion_schedule','residency_evidence')),
  object_id TEXT NOT NULL CHECK(length(object_id) BETWEEN 4 AND 80),
  classification_code TEXT CHECK(classification_code IS NULL OR classification_code IN(
    'prompt','response','file','audit','identity','billing')),
  policy_generation INTEGER NOT NULL CHECK(policy_generation>=1),
  status_code TEXT NOT NULL CHECK(length(status_code) BETWEEN 3 AND 96
    AND status_code=lower(status_code) AND status_code NOT GLOB '*[^a-z0-9._-]*'),
  item_count INTEGER NOT NULL CHECK(item_count>=0),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,data_governance_event_id)
);
CREATE TABLE data_governance_event_outbox(
  data_governance_event_outbox_id TEXT PRIMARY KEY CHECK(length(data_governance_event_outbox_id)=36
    AND substr(data_governance_event_outbox_id,1,4)='dgo_'),
  data_governance_event_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  delivery_status TEXT NOT NULL CHECK(delivery_status IN('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,data_governance_event_id)
    REFERENCES data_governance_events(workspace_id,data_governance_event_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_data_governance_outbox_delivery_prd117
  ON data_governance_event_outbox(delivery_status,next_attempt_at,data_governance_event_outbox_id);
CREATE TRIGGER trg_data_governance_events_update_prd117 BEFORE UPDATE ON data_governance_events
BEGIN SELECT RAISE(ABORT,'data_governance_event_immutable'); END;
CREATE TRIGGER trg_data_governance_events_delete_prd117 BEFORE DELETE ON data_governance_events
BEGIN SELECT RAISE(ABORT,'data_governance_event_immutable'); END;
CREATE TRIGGER trg_data_governance_outbox_transition_prd117
BEFORE UPDATE ON data_governance_event_outbox FOR EACH ROW
WHEN NEW.data_governance_event_outbox_id!=OLD.data_governance_event_outbox_id
  OR NEW.data_governance_event_id!=OLD.data_governance_event_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at OR NEW.attempt_count!=OLD.attempt_count+1
  OR NEW.updated_at<=OLD.updated_at
  OR OLD.delivery_status='delivered' OR NEW.delivery_status NOT IN('pending','delivered','failed')
  OR (NEW.delivery_status='delivered' AND NEW.delivered_at IS NULL)
  OR (NEW.delivery_status!='delivered' AND NEW.delivered_at IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'data_governance_outbox_transition_invalid'); END;
CREATE TRIGGER trg_data_governance_outbox_delete_prd117 BEFORE DELETE ON data_governance_event_outbox
BEGIN SELECT RAISE(ABORT,'data_governance_event_immutable'); END;

ALTER TABLE workspace_gateway_placements ADD COLUMN governance_generation INTEGER NOT NULL DEFAULT 1
  CHECK(governance_generation>=1);
ALTER TABLE backup_sets ADD COLUMN governance_generation INTEGER CHECK(governance_generation IS NULL
  OR governance_generation>=1);
ALTER TABLE backup_sets ADD COLUMN residency_group TEXT CHECK(residency_group IS NULL
  OR(length(residency_group) BETWEEN 3 AND 40 AND residency_group=lower(residency_group)
    AND residency_group NOT GLOB '*[^a-z0-9-]*'));

INSERT INTO platform_job_types(
  type_code,contract_version,artifact_sha256,queue_code,resource_class,max_attempts,
  timeout_seconds,status,revision,created_at,updated_at)
VALUES('data.retention.delete',1,
  '0c206e64a14148850329c8e633a0a4362e134ba693baa160215faec935927783',
  'bulk','io',5,3600,'active',1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TRIGGER trg_workspace_regions_governance_seed_prd117
AFTER INSERT ON workspace_regions FOR EACH ROW
WHEN NEW.status='active'
BEGIN
  INSERT INTO data_classifications(
    data_classification_id,workspace_id,tenant_id,code,sensitivity_level,
    allowed_purposes_json,default_retention_days,catalog_revision,status,created_at)
  SELECT 'dcl_'||lower(hex(randomblob(16))),NEW.workspace_id,NEW.tenant_id,value,
    CASE value WHEN 'identity' THEN 'restricted' WHEN 'billing' THEN 'confidential'
      WHEN 'audit' THEN 'confidential' ELSE 'restricted' END,
    CASE value WHEN 'identity' THEN json_array('authentication','security')
      WHEN 'billing' THEN json_array('billing','compliance')
      WHEN 'audit' THEN json_array('security','compliance')
      ELSE json_array('service_delivery','security') END,
    CASE value WHEN 'prompt' THEN 30 WHEN 'response' THEN 30 WHEN 'file' THEN 90
      WHEN 'audit' THEN 365 WHEN 'identity' THEN 365 WHEN 'billing' THEN 2555 END,
    1,'active',strftime('%Y-%m-%dT%H:%M:%fZ','now')
  FROM json_each(json_array('prompt','response','file','audit','identity','billing'));
  INSERT INTO data_governance_fences VALUES(
    'dgf_'||lower(hex(randomblob(16))),NEW.workspace_id,NEW.tenant_id,1,0,0,1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'));
  INSERT INTO residency_policies(
    residency_policy_id,workspace_id,tenant_id,allowed_residency_groups_json,
    primary_residency_group,cross_region_mode,status,revision,generation,policy_sha256,
    reason_code,updated_by_account_id,valid_until,superseded_at,created_at,updated_at)
  SELECT 'rgp_'||lower(hex(randomblob(16))),NEW.workspace_id,NEW.tenant_id,
    json_array(region.residency_group),region.residency_group,'same_group','active',1,1,
    metahub_sha256(json_object('workspace_id',NEW.workspace_id,'tenant_id',NEW.tenant_id,
      'generation',1,'allowed_residency_groups',json_array(region.residency_group),
      'primary_residency_group',region.residency_group,'cross_region_mode','same_group')),
    'initial_policy',NULL,strftime('%Y-%m-%dT%H:%M:%fZ','now','+3650 days'),NULL,
    strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now')
  FROM platform_regions region WHERE region.region_code=NEW.region_code;
  INSERT INTO retention_policies(
    retention_policy_id,workspace_id,tenant_id,data_classification_id,classification_code,
    retention_days,deletion_mode,backup_grace_days,status,revision,generation,
    superseded_at,created_at,updated_at)
  SELECT 'rtp_'||lower(hex(randomblob(16))),classification.workspace_id,
    classification.tenant_id,classification.data_classification_id,classification.code,
    classification.default_retention_days,
    CASE WHEN classification.code IN('audit','billing') THEN 'archive_then_delete'
      WHEN classification.code='identity' THEN 'anonymize' ELSE 'hard_delete' END,
    CASE WHEN classification.code IN('audit','billing') THEN 30 ELSE 7 END,
    'active',1,1,NULL,strftime('%Y-%m-%dT%H:%M:%fZ','now'),
    strftime('%Y-%m-%dT%H:%M:%fZ','now')
  FROM data_classifications classification WHERE classification.workspace_id=NEW.workspace_id;
END;

INSERT INTO data_classifications(
  data_classification_id,workspace_id,tenant_id,code,sensitivity_level,
  allowed_purposes_json,default_retention_days,catalog_revision,status,created_at)
SELECT 'dcl_'||lower(hex(randomblob(16))),region.workspace_id,region.tenant_id,code.value,
  CASE code.value WHEN 'identity' THEN 'restricted' WHEN 'billing' THEN 'confidential'
    WHEN 'audit' THEN 'confidential' ELSE 'restricted' END,
  CASE code.value WHEN 'identity' THEN json_array('authentication','security')
    WHEN 'billing' THEN json_array('billing','compliance')
    WHEN 'audit' THEN json_array('security','compliance')
    ELSE json_array('service_delivery','security') END,
  CASE code.value WHEN 'prompt' THEN 30 WHEN 'response' THEN 30 WHEN 'file' THEN 90
    WHEN 'audit' THEN 365 WHEN 'identity' THEN 365 WHEN 'billing' THEN 2555 END,
  1,'active',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM workspace_regions region
JOIN json_each(json_array('prompt','response','file','audit','identity','billing')) code
WHERE region.status='active';
INSERT INTO data_governance_fences
SELECT 'dgf_'||lower(hex(randomblob(16))),region.workspace_id,region.tenant_id,1,0,0,1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM workspace_regions region WHERE region.status='active';
INSERT INTO residency_policies(
  residency_policy_id,workspace_id,tenant_id,allowed_residency_groups_json,
  primary_residency_group,cross_region_mode,status,revision,generation,policy_sha256,
  reason_code,updated_by_account_id,valid_until,superseded_at,created_at,updated_at)
SELECT 'rgp_'||lower(hex(randomblob(16))),workspace_region.workspace_id,
  workspace_region.tenant_id,json_array(platform_region.residency_group),
  platform_region.residency_group,'same_group','active',1,1,
  metahub_sha256(json_object('workspace_id',workspace_region.workspace_id,
    'tenant_id',workspace_region.tenant_id,'generation',1,
    'allowed_residency_groups',json_array(platform_region.residency_group),
    'primary_residency_group',platform_region.residency_group,'cross_region_mode','same_group')),
  'initial_policy',NULL,strftime('%Y-%m-%dT%H:%M:%fZ','now','+3650 days'),NULL,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM workspace_regions workspace_region
JOIN platform_regions platform_region ON platform_region.region_code=workspace_region.region_code
WHERE workspace_region.status='active';
INSERT INTO retention_policies(
  retention_policy_id,workspace_id,tenant_id,data_classification_id,classification_code,
  retention_days,deletion_mode,backup_grace_days,status,revision,generation,
  superseded_at,created_at,updated_at)
SELECT 'rtp_'||lower(hex(randomblob(16))),classification.workspace_id,
  classification.tenant_id,classification.data_classification_id,classification.code,
  classification.default_retention_days,
  CASE WHEN classification.code IN('audit','billing') THEN 'archive_then_delete'
    WHEN classification.code='identity' THEN 'anonymize' ELSE 'hard_delete' END,
  CASE WHEN classification.code IN('audit','billing') THEN 30 ELSE 7 END,
  'active',1,1,NULL,strftime('%Y-%m-%dT%H:%M:%fZ','now'),
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM data_classifications classification;

-- Later migrations legitimately changed tenant-owned tables and triggers. Refresh the
-- complete PRD-089 deletion inventory atomically so destructive deletion remains fail-closed.
CREATE TABLE tenant_deletion_manifest_extensions(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(length(tenant_key_column) BETWEEN 1 AND 128
    AND tenant_key_column NOT GLOB '*[^A-Za-z0-9_]*'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-117'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;

CREATE TEMP TABLE prd117_manifest_extension_sources AS
SELECT table_record.name AS source_table,
  CASE WHEN table_record.name='account_identity_links'
    THEN 'legacy_tenant_id' ELSE 'tenant_id' END AS tenant_key_column,
  CASE WHEN table_record.name IN(
      'workspace_tenant_bindings','data_classifications','data_governance_fences',
      'residency_policies','retention_policies','legal_holds','deletion_schedules',
      'residency_evidence','data_governance_commands','data_governance_events',
      'data_governance_event_outbox') THEN 'preserve'
    WHEN table_record.name LIKE 'billing_%' OR table_record.name LIKE 'invoice_%'
      OR table_record.name LIKE 'payment_%' OR table_record.name LIKE 'platform_billing_%'
      OR table_record.name IN(
        'commercial_effects','contract_profiles','dunning_cases','legal_entity_profiles',
        'platform_meter_readings','reconciliation_differences','refunds',
        'subscription_order_records','subscription_orders','workspace_billing_profiles')
      THEN 'archive_then_delete'
    WHEN table_record.name LIKE 'risk_%' OR table_record.name LIKE 'security_%'
      OR table_record.name LIKE '%_events' OR table_record.name LIKE '%_event_outbox'
      OR table_record.name LIKE '%_evidence' OR table_record.name IN(
        'break_glass_sessions','business_verification_intents',
        'conditional_access_simulations','identity_security_events','restore_evidence',
        'support_sessions','verification_evidence') THEN 'archive_then_delete'
    ELSE 'delete' END AS disposition,
  CASE WHEN table_record.name LIKE 'billing_%' OR table_record.name LIKE 'invoice_%'
      OR table_record.name LIKE 'payment_%' OR table_record.name LIKE 'platform_billing_%'
      OR table_record.name IN(
        'commercial_effects','contract_profiles','dunning_cases','legal_entity_profiles',
        'platform_meter_readings','reconciliation_differences','refunds',
        'subscription_order_records','subscription_orders','workspace_billing_profiles')
      THEN 'billing'
    WHEN table_record.name LIKE 'risk_%' OR table_record.name LIKE 'security_%'
      OR table_record.name IN(
        'break_glass_sessions','conditional_access_simulations','identity_security_events',
        'support_sessions') THEN 'security'
    WHEN table_record.name LIKE '%_events' OR table_record.name LIKE '%_event_outbox'
      OR table_record.name LIKE '%_evidence' OR table_record.name IN(
        'business_verification_intents','restore_evidence','verification_evidence') THEN 'audit'
    ELSE NULL END AS retention_class
FROM sqlite_schema table_record
WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
  AND (table_record.name='account_identity_links'
    OR EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id'))
  AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
    WHERE manifest.source_table=table_record.name);

INSERT INTO tenant_deletion_table_manifest(
  source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
  trigger_set_sha256,catalog_revision,created_at)
SELECT table_record.name,extension.disposition,extension.retention_class,
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
JOIN prd117_manifest_extension_sources extension
  ON extension.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,tenant_key_column,'PRD-117',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd117_manifest_extension_sources ORDER BY source_table;
DROP TABLE prd117_manifest_extension_sources;

CREATE TEMP TABLE prd117_manifest_refresh AS
SELECT table_record.name AS source_table,metahub_sha256(table_record.sql) AS table_sql_sha256,
  metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info(table_record.name) ORDER BY cid))) AS column_signature_sha256,
  metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT trigger_record.name||char(31)||metahub_sha256(trigger_record.sql) AS record
    FROM sqlite_schema trigger_record WHERE trigger_record.type='trigger'
      AND trigger_record.tbl_name=table_record.name ORDER BY trigger_record.name)),''))
    AS trigger_set_sha256
FROM sqlite_schema table_record
JOIN tenant_deletion_table_manifest manifest ON manifest.source_table=table_record.name
WHERE table_record.type='table';
CREATE TEMP TABLE prd117_manifest_refresh_guard(
  issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd117_manifest_refresh_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM prd117_manifest_refresh)=
    (SELECT COUNT(*) FROM tenant_deletion_table_manifest) THEN 0 ELSE 1 END;
DROP TABLE prd117_manifest_refresh_guard;
UPDATE tenant_deletion_table_manifest
SET table_sql_sha256=(SELECT refreshed.table_sql_sha256 FROM prd117_manifest_refresh refreshed
      WHERE refreshed.source_table=tenant_deletion_table_manifest.source_table),
    column_signature_sha256=(SELECT refreshed.column_signature_sha256 FROM prd117_manifest_refresh refreshed
      WHERE refreshed.source_table=tenant_deletion_table_manifest.source_table),
    trigger_set_sha256=(SELECT refreshed.trigger_set_sha256 FROM prd117_manifest_refresh refreshed
      WHERE refreshed.source_table=tenant_deletion_table_manifest.source_table);
DROP TABLE prd117_manifest_refresh;

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
CREATE TRIGGER trg_tenant_deletion_manifest_extensions_insert_prd117
BEFORE INSERT ON tenant_deletion_manifest_extensions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_extensions_update_prd117
BEFORE UPDATE ON tenant_deletion_manifest_extensions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_extensions_delete_prd117
BEFORE DELETE ON tenant_deletion_manifest_extensions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd117_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd117_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_governance_permission_extensions)=4
    AND (SELECT COUNT(*) FROM workspace_governance_role_permissions)=12 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_data_governance_permission_extensions)=1
    AND (SELECT COUNT(*) FROM platform_data_governance_role_permissions)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM data_classifications)=
      (SELECT COUNT(*)*6 FROM workspace_regions WHERE status='active') THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM residency_policies WHERE status='active')=
      (SELECT COUNT(*) FROM workspace_regions WHERE status='active') THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM retention_policies WHERE status='active')=
      (SELECT COUNT(*)*6 FROM workspace_regions WHERE status='active') THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'data_classifications','data_governance_fences','residency_policies','retention_policies',
      'legal_holds','deletion_schedules','residency_evidence','data_governance_commands',
      'data_governance_events','data_governance_event_outbox'))=10 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name))
      AND EXISTS(SELECT 1 FROM tenant_deletion_manifest_extensions
        WHERE source_table='account_identity_links'
          AND tenant_key_column='legacy_tenant_id') THEN 0 ELSE 1 END
  + CASE WHEN (SELECT artifact_sha256 FROM platform_job_types
      WHERE type_code='data.retention.delete')=
      '0c206e64a14148850329c8e633a0a4362e134ba693baa160215faec935927783'
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('workspace_gateway_placements')
      WHERE name='governance_generation')=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('backup_sets')
      WHERE name IN('governance_generation','residency_group'))=2 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd117_up_final_guard;
COMMIT;
