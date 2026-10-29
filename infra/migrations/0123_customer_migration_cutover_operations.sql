-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd131_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd131_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0122' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_tenant_bindings','workspace_memberships','global_accounts','support_sessions',
      'support_grants','tenant_deletion_table_manifest'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd131')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd131_up_guard;

CREATE TABLE workspace_migration_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'migration.read','migration.manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-131'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=43)
);
CREATE TABLE workspace_migration_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code) REFERENCES workspace_migration_permission_extensions(permission_code)
);
INSERT INTO workspace_migration_permission_extensions VALUES
  ('migration.read','PRD-131',43),('migration.manage','PRD-131',43);
INSERT INTO workspace_migration_role_permissions VALUES
  ('owner','migration.read'),('owner','migration.manage'),
  ('admin','migration.read'),('admin','migration.manage'),
  ('developer','migration.read');
CREATE TRIGGER trg_workspace_migration_permissions_insert_prd131
BEFORE INSERT ON workspace_migration_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_migration_permission_immutable'); END;
CREATE TRIGGER trg_workspace_migration_permissions_update_prd131
BEFORE UPDATE ON workspace_migration_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_migration_permission_immutable'); END;
CREATE TRIGGER trg_workspace_migration_permissions_delete_prd131
BEFORE DELETE ON workspace_migration_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_migration_permission_immutable'); END;
CREATE TRIGGER trg_workspace_migration_roles_insert_prd131
BEFORE INSERT ON workspace_migration_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_migration_permission_immutable'); END;
CREATE TRIGGER trg_workspace_migration_roles_update_prd131
BEFORE UPDATE ON workspace_migration_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_migration_permission_immutable'); END;
CREATE TRIGGER trg_workspace_migration_roles_delete_prd131
BEFORE DELETE ON workspace_migration_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_migration_permission_immutable'); END;

CREATE TABLE migration_source_adapters(
  domain_code TEXT PRIMARY KEY CHECK(domain_code IN(
    'identity','workspace','provider_reference','policy','ledger','audit','edge')),
  adapter_version TEXT NOT NULL CHECK(adapter_version='migration-adapter-v1'),
  owner_kind TEXT NOT NULL CHECK(owner_kind='workspace_tenant'),
  source_kind_json TEXT NOT NULL CHECK(json_valid(source_kind_json)
    AND json_type(source_kind_json)='array' AND json_array_length(source_kind_json)=3),
  projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256)=64
    AND projection_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-131')
);
INSERT INTO migration_source_adapters VALUES
  ('identity','migration-adapter-v1','workspace_tenant',
    '["direct_provider","legacy_tenant","private_deployment"]',
    '0f7df9e460f116edcd1b6edc50d97c4df383e4a1bd06d8055dc99341441db9bb','PRD-131'),
  ('workspace','migration-adapter-v1','workspace_tenant',
    '["direct_provider","legacy_tenant","private_deployment"]',
    'a1df8dc2e7a901265910bc027743e9a97cf33ce0203d58db5d6749ee7f308f20','PRD-131'),
  ('provider_reference','migration-adapter-v1','workspace_tenant',
    '["direct_provider","legacy_tenant","private_deployment"]',
    '6ad7ed50277dc49b8c9126c3aebd7f2090be43d29fb5a610209541cfc17def94','PRD-131'),
  ('policy','migration-adapter-v1','workspace_tenant',
    '["direct_provider","legacy_tenant","private_deployment"]',
    '438e187952c64c4e99de8087c20219aff7d0a2cd6b8a858f22c8db1b93b45fd9','PRD-131'),
  ('ledger','migration-adapter-v1','workspace_tenant',
    '["direct_provider","legacy_tenant","private_deployment"]',
    'fa3e182df81f651ca54dc74f28fe4d8aa17c461a6db7db601d8d914082778f37','PRD-131'),
  ('audit','migration-adapter-v1','workspace_tenant',
    '["direct_provider","legacy_tenant","private_deployment"]',
    '546eef0ead6647d96550b499bce50f6e5948da6c98c80bff7dc2458934aca2cd','PRD-131'),
  ('edge','migration-adapter-v1','workspace_tenant',
    '["direct_provider","legacy_tenant","private_deployment"]',
    '993322170b769c8db6a3e4b4d985249b9a0ecffb54084bb76008261fcc8c6d68','PRD-131');
CREATE TRIGGER trg_migration_source_adapters_insert_prd131
BEFORE INSERT ON migration_source_adapters
BEGIN SELECT RAISE(ABORT,'migration_adapter_immutable'); END;
CREATE TRIGGER trg_migration_source_adapters_update_prd131
BEFORE UPDATE ON migration_source_adapters
BEGIN SELECT RAISE(ABORT,'migration_adapter_immutable'); END;
CREATE TRIGGER trg_migration_source_adapters_delete_prd131
BEFORE DELETE ON migration_source_adapters
BEGIN SELECT RAISE(ABORT,'migration_adapter_immutable'); END;

CREATE TABLE migration_assessments(
  migration_assessment_id TEXT PRIMARY KEY CHECK(length(migration_assessment_id)=36
    AND substr(migration_assessment_id,1,4)='mas_'
    AND substr(migration_assessment_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN(
    'legacy_tenant','direct_provider','private_deployment')),
  manifest_version TEXT NOT NULL CHECK(length(manifest_version) BETWEEN 3 AND 40
    AND manifest_version=lower(manifest_version)
    AND manifest_version NOT GLOB '*[^a-z0-9._-]*'),
  source_manifest_sha256 TEXT NOT NULL CHECK(length(source_manifest_sha256)=64
    AND source_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  domain_set_sha256 TEXT NOT NULL CHECK(length(domain_set_sha256)=64
    AND domain_set_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('ready','validation_failed')),
  object_count INTEGER NOT NULL CHECK(object_count BETWEEN 0 AND 1000000000),
  blocking_difference_count INTEGER NOT NULL CHECK(blocking_difference_count BETWEEN 0 AND 1000000),
  write_watermark_before INTEGER NOT NULL CHECK(write_watermark_before>=0),
  write_watermark_after INTEGER NOT NULL CHECK(write_watermark_after>=0),
  credential_reentry_required_count INTEGER NOT NULL CHECK(
    credential_reentry_required_count BETWEEN 0 AND 1000000),
  permission_expansion_count INTEGER NOT NULL CHECK(permission_expansion_count BETWEEN 0 AND 1000000),
  requested_by_account_id TEXT NOT NULL,
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9._-]*'),
  revision INTEGER NOT NULL CHECK(revision=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  completed_at TEXT NOT NULL CHECK(unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(requested_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,migration_assessment_id),
  CHECK(write_watermark_before=write_watermark_after),
  CHECK((status='ready' AND blocking_difference_count=0 AND permission_expansion_count=0)
    OR(status='validation_failed' AND blocking_difference_count>0))
);
CREATE INDEX idx_migration_assessments_owner_prd131
  ON migration_assessments(workspace_id,created_at DESC,migration_assessment_id DESC);
CREATE TRIGGER trg_migration_assessments_update_prd131
BEFORE UPDATE ON migration_assessments
BEGIN SELECT RAISE(ABORT,'migration_assessment_immutable'); END;
CREATE TRIGGER trg_migration_assessments_delete_prd131
BEFORE DELETE ON migration_assessments
BEGIN SELECT RAISE(ABORT,'migration_assessment_immutable'); END;

CREATE TABLE migration_plans(
  migration_plan_id TEXT PRIMARY KEY CHECK(length(migration_plan_id)=36
    AND substr(migration_plan_id,1,4)='mpl_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  migration_assessment_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL CHECK(generation=1),
  status TEXT NOT NULL CHECK(status IN('ready','blocked')),
  phase_set_json TEXT NOT NULL CHECK(json_valid(phase_set_json)
    AND json_type(phase_set_json)='array' AND json_array_length(phase_set_json)=6),
  plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64),
  planned_downtime_seconds INTEGER NOT NULL CHECK(planned_downtime_seconds BETWEEN 0 AND 7200),
  rollback_deadline_seconds INTEGER NOT NULL CHECK(rollback_deadline_seconds BETWEEN 1 AND 7200),
  revision INTEGER NOT NULL CHECK(revision=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,migration_assessment_id)
    REFERENCES migration_assessments(workspace_id,tenant_id,migration_assessment_id),
  UNIQUE(workspace_id,tenant_id,migration_plan_id)
);
CREATE TRIGGER trg_migration_plans_update_prd131
BEFORE UPDATE ON migration_plans
BEGIN SELECT RAISE(ABORT,'migration_plan_immutable'); END;
CREATE TRIGGER trg_migration_plans_delete_prd131
BEFORE DELETE ON migration_plans
BEGIN SELECT RAISE(ABORT,'migration_plan_immutable'); END;

CREATE TABLE migration_assessment_snapshots(
  migration_assessment_snapshot_id TEXT PRIMARY KEY CHECK(
    length(migration_assessment_snapshot_id)=36
    AND substr(migration_assessment_snapshot_id,1,4)='msp_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  migration_assessment_id TEXT NOT NULL,
  domain_code TEXT NOT NULL,
  adapter_version TEXT NOT NULL CHECK(adapter_version='migration-adapter-v1'),
  source_count INTEGER NOT NULL CHECK(source_count>=0),
  target_count INTEGER NOT NULL CHECK(target_count>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  target_sha256 TEXT NOT NULL CHECK(length(target_sha256)=64),
  source_sequence INTEGER NOT NULL CHECK(source_sequence>=0),
  target_sequence INTEGER NOT NULL CHECK(target_sequence>=0),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=0),
  target_watermark INTEGER NOT NULL CHECK(target_watermark>=0),
  matches INTEGER NOT NULL CHECK(matches IN(0,1)),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,migration_assessment_id)
    REFERENCES migration_assessments(workspace_id,tenant_id,migration_assessment_id),
  FOREIGN KEY(domain_code) REFERENCES migration_source_adapters(domain_code),
  UNIQUE(migration_assessment_id,domain_code),
  CHECK(matches=CASE WHEN source_count=target_count AND source_sha256=target_sha256
    AND source_sequence=target_sequence AND source_watermark=target_watermark THEN 1 ELSE 0 END)
);
CREATE TRIGGER trg_migration_snapshots_update_prd131
BEFORE UPDATE ON migration_assessment_snapshots
BEGIN SELECT RAISE(ABORT,'migration_snapshot_immutable'); END;
CREATE TRIGGER trg_migration_snapshots_delete_prd131
BEFORE DELETE ON migration_assessment_snapshots
BEGIN SELECT RAISE(ABORT,'migration_snapshot_immutable'); END;

CREATE TABLE migration_runs(
  migration_run_id TEXT PRIMARY KEY CHECK(length(migration_run_id)=36
    AND substr(migration_run_id,1,4)='mrn_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  migration_assessment_id TEXT NOT NULL,
  migration_plan_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN(
    'observing','completed','failed','rolling_back','rolled_back')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  freeze_generation INTEGER NOT NULL CHECK(freeze_generation>=1),
  source_manifest_sha256 TEXT NOT NULL CHECK(length(source_manifest_sha256)=64),
  authorization_fingerprint_sha256 TEXT NOT NULL CHECK(length(authorization_fingerprint_sha256)=64),
  requested_by_account_id TEXT NOT NULL,
  blocking_difference_count INTEGER NOT NULL CHECK(blocking_difference_count BETWEEN 0 AND 1000000),
  revision INTEGER NOT NULL CHECK(revision>=1),
  started_at TEXT NOT NULL CHECK(unixepoch(started_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,migration_assessment_id)
    REFERENCES migration_assessments(workspace_id,tenant_id,migration_assessment_id),
  FOREIGN KEY(workspace_id,tenant_id,migration_plan_id)
    REFERENCES migration_plans(workspace_id,tenant_id,migration_plan_id),
  FOREIGN KEY(requested_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,migration_run_id),
  CHECK((status IN('completed','rolled_back') AND completed_at IS NOT NULL)
    OR(status NOT IN('completed','rolled_back') AND completed_at IS NULL))
);
CREATE INDEX idx_migration_runs_owner_prd131
  ON migration_runs(workspace_id,started_at DESC,migration_run_id DESC);
CREATE TRIGGER trg_migration_runs_update_prd131
BEFORE UPDATE ON migration_runs FOR EACH ROW
WHEN NEW.migration_run_id!=OLD.migration_run_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.migration_assessment_id!=OLD.migration_assessment_id
  OR NEW.migration_plan_id!=OLD.migration_plan_id OR NEW.generation!=OLD.generation
  OR NEW.freeze_generation!=OLD.freeze_generation
  OR NEW.source_manifest_sha256!=OLD.source_manifest_sha256
  OR NEW.authorization_fingerprint_sha256!=OLD.authorization_fingerprint_sha256
  OR NEW.requested_by_account_id!=OLD.requested_by_account_id
  OR NEW.started_at!=OLD.started_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR NOT ((OLD.status='observing' AND NEW.status IN('completed','failed','rolling_back'))
    OR(OLD.status IN('completed','failed') AND NEW.status='rolling_back')
    OR(OLD.status='rolling_back' AND NEW.status IN('rolled_back','failed')))
BEGIN SELECT RAISE(ABORT,'migration_run_transition_invalid'); END;
CREATE TRIGGER trg_migration_runs_delete_prd131
BEFORE DELETE ON migration_runs
BEGIN SELECT RAISE(ABORT,'migration_run_immutable'); END;

CREATE TABLE migration_differences(
  migration_difference_id TEXT PRIMARY KEY CHECK(length(migration_difference_id)=36
    AND substr(migration_difference_id,1,4)='mdf_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  migration_assessment_id TEXT NOT NULL,
  migration_run_id TEXT,
  domain_code TEXT NOT NULL,
  difference_code TEXT NOT NULL CHECK(difference_code IN(
    'count_mismatch','hash_mismatch','sequence_mismatch','watermark_mismatch',
    'permission_expansion')),
  severity TEXT NOT NULL CHECK(severity='blocking'),
  source_count INTEGER NOT NULL CHECK(source_count>=0),
  target_count INTEGER NOT NULL CHECK(target_count>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  target_sha256 TEXT NOT NULL CHECK(length(target_sha256)=64),
  source_sequence INTEGER NOT NULL CHECK(source_sequence>=0),
  target_sequence INTEGER NOT NULL CHECK(target_sequence>=0),
  remediation_code TEXT NOT NULL CHECK(remediation_code IN(
    'rebuild_target_projection','replay_missing_sequence','wait_for_watermark',
    'reassess_source','review_role_mapping')),
  status TEXT NOT NULL CHECK(status='open'),
  detected_at TEXT NOT NULL CHECK(unixepoch(detected_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,migration_assessment_id)
    REFERENCES migration_assessments(workspace_id,tenant_id,migration_assessment_id),
  FOREIGN KEY(workspace_id,tenant_id,migration_run_id)
    REFERENCES migration_runs(workspace_id,tenant_id,migration_run_id),
  FOREIGN KEY(domain_code) REFERENCES migration_source_adapters(domain_code),
  UNIQUE(migration_assessment_id,migration_run_id,domain_code,difference_code)
);
CREATE INDEX idx_migration_differences_owner_prd131
  ON migration_differences(workspace_id,migration_assessment_id,migration_run_id,domain_code);
CREATE TRIGGER trg_migration_differences_update_prd131
BEFORE UPDATE ON migration_differences
BEGIN SELECT RAISE(ABORT,'migration_difference_immutable'); END;
CREATE TRIGGER trg_migration_differences_delete_prd131
BEFORE DELETE ON migration_differences
BEGIN SELECT RAISE(ABORT,'migration_difference_immutable'); END;

CREATE TABLE migration_cutover_checkpoints(
  migration_cutover_checkpoint_id TEXT PRIMARY KEY CHECK(
    length(migration_cutover_checkpoint_id)=36
    AND substr(migration_cutover_checkpoint_id,1,4)='mcp_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  migration_run_id TEXT NOT NULL,
  phase_code TEXT NOT NULL CHECK(phase_code IN(
    'assessment','backfill','dual_read','freeze','cutover','rollback')),
  phase_sequence INTEGER NOT NULL CHECK(phase_sequence BETWEEN 1 AND 6),
  input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
  output_sha256 TEXT NOT NULL CHECK(length(output_sha256)=64),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=0),
  target_watermark INTEGER NOT NULL CHECK(target_watermark>=0),
  freeze_generation INTEGER NOT NULL CHECK(freeze_generation>=1),
  status TEXT NOT NULL CHECK(status IN('verified','failed')),
  worker_fencing_token_sha256 TEXT NOT NULL CHECK(length(worker_fencing_token_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,migration_run_id)
    REFERENCES migration_runs(workspace_id,tenant_id,migration_run_id),
  UNIQUE(migration_run_id,phase_code)
);
CREATE TRIGGER trg_migration_checkpoints_update_prd131
BEFORE UPDATE ON migration_cutover_checkpoints
BEGIN SELECT RAISE(ABORT,'migration_checkpoint_immutable'); END;
CREATE TRIGGER trg_migration_checkpoints_delete_prd131
BEFORE DELETE ON migration_cutover_checkpoints
BEGIN SELECT RAISE(ABORT,'migration_checkpoint_immutable'); END;

CREATE TABLE migration_rollback_plans(
  migration_rollback_plan_id TEXT PRIMARY KEY CHECK(length(migration_rollback_plan_id)=36
    AND substr(migration_rollback_plan_id,1,4)='mrp_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  migration_run_id TEXT NOT NULL UNIQUE,
  previous_migration_run_id TEXT,
  last_safe_checkpoint_id TEXT NOT NULL,
  compensation_order_json TEXT NOT NULL CHECK(json_valid(compensation_order_json)
    AND json_type(compensation_order_json)='array' AND json_array_length(compensation_order_json)=4),
  rollback_plan_sha256 TEXT NOT NULL CHECK(length(rollback_plan_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('ready','completed','failed')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,migration_run_id)
    REFERENCES migration_runs(workspace_id,tenant_id,migration_run_id),
  FOREIGN KEY(workspace_id,tenant_id,previous_migration_run_id)
    REFERENCES migration_runs(workspace_id,tenant_id,migration_run_id),
  FOREIGN KEY(last_safe_checkpoint_id) REFERENCES migration_cutover_checkpoints(
    migration_cutover_checkpoint_id),
  UNIQUE(workspace_id,tenant_id,migration_rollback_plan_id),
  CHECK((status='ready' AND completed_at IS NULL)
    OR(status IN('completed','failed') AND completed_at IS NOT NULL))
);
CREATE TRIGGER trg_migration_rollback_plans_update_prd131
BEFORE UPDATE ON migration_rollback_plans FOR EACH ROW
WHEN NEW.migration_rollback_plan_id!=OLD.migration_rollback_plan_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.migration_run_id!=OLD.migration_run_id
  OR NEW.previous_migration_run_id IS NOT OLD.previous_migration_run_id
  OR NEW.last_safe_checkpoint_id!=OLD.last_safe_checkpoint_id
  OR NEW.compensation_order_json!=OLD.compensation_order_json
  OR NEW.rollback_plan_sha256!=OLD.rollback_plan_sha256
  OR OLD.status!='ready' OR NEW.status NOT IN('completed','failed')
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR NEW.completed_at IS NULL
BEGIN SELECT RAISE(ABORT,'migration_rollback_transition_invalid'); END;
CREATE TRIGGER trg_migration_rollback_plans_delete_prd131
BEFORE DELETE ON migration_rollback_plans
BEGIN SELECT RAISE(ABORT,'migration_rollback_plan_immutable'); END;

CREATE TABLE migration_cutover_heads(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN('legacy','workspace')),
  active_migration_run_id TEXT,
  generation INTEGER NOT NULL CHECK(generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id,active_migration_run_id)
    REFERENCES migration_runs(workspace_id,tenant_id,migration_run_id),
  CHECK((mode='legacy' AND active_migration_run_id IS NULL)
    OR(mode='workspace' AND active_migration_run_id IS NOT NULL))
);
CREATE TRIGGER trg_migration_cutover_heads_update_prd131
BEFORE UPDATE ON migration_cutover_heads FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.generation!=OLD.generation+1 OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'migration_cutover_head_transition_invalid'); END;
CREATE TRIGGER trg_migration_cutover_heads_delete_prd131
BEFORE DELETE ON migration_cutover_heads
BEGIN SELECT RAISE(ABORT,'migration_cutover_head_immutable'); END;

CREATE TABLE migration_events(
  migration_event_id TEXT PRIMARY KEY CHECK(length(migration_event_id)=36
    AND substr(migration_event_id,1,4)='mev_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  migration_assessment_id TEXT NOT NULL,
  migration_run_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'migration.started','migration.validation_failed',
    'migration.cutover_completed','migration.rolled_back')),
  actor_account_id TEXT NOT NULL,
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9._-]*'),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,migration_assessment_id)
    REFERENCES migration_assessments(workspace_id,tenant_id,migration_assessment_id),
  FOREIGN KEY(workspace_id,tenant_id,migration_run_id)
    REFERENCES migration_runs(workspace_id,tenant_id,migration_run_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,migration_event_id)
);
CREATE TRIGGER trg_migration_events_update_prd131
BEFORE UPDATE ON migration_events
BEGIN SELECT RAISE(ABORT,'migration_event_immutable'); END;
CREATE TRIGGER trg_migration_events_delete_prd131
BEFORE DELETE ON migration_events
BEGIN SELECT RAISE(ABORT,'migration_event_immutable'); END;

CREATE TABLE migration_event_outbox(
  migration_event_outbox_id TEXT PRIMARY KEY CHECK(length(migration_event_outbox_id)=36
    AND substr(migration_event_outbox_id,1,4)='mob_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  migration_event_id TEXT NOT NULL,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('audit','migration')),
  status TEXT NOT NULL CHECK(status IN('pending','delivered')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 1000),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,migration_event_id)
    REFERENCES migration_events(workspace_id,tenant_id,migration_event_id),
  UNIQUE(migration_event_id,delivery_domain)
);
CREATE TRIGGER trg_migration_outbox_update_prd131
BEFORE UPDATE ON migration_event_outbox FOR EACH ROW
WHEN NEW.migration_event_outbox_id!=OLD.migration_event_outbox_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.migration_event_id!=OLD.migration_event_id
  OR NEW.delivery_domain!=OLD.delivery_domain OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count!=OLD.attempt_count+1 OR NEW.updated_at<=OLD.updated_at
  OR OLD.status='delivered'
BEGIN SELECT RAISE(ABORT,'migration_outbox_transition_invalid'); END;
CREATE TRIGGER trg_migration_outbox_delete_prd131
BEFORE DELETE ON migration_event_outbox
BEGIN SELECT RAISE(ABORT,'migration_outbox_immutable'); END;

CREATE TABLE migration_action_commands(
  migration_action_command_id TEXT PRIMARY KEY CHECK(length(migration_action_command_id)=36
    AND substr(migration_action_command_id,1,4)='mac_'),
  actor_account_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'migration.assessment.create','migration.run.create',
    'migration.cutover.execute','migration.rollback.execute')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(actor_account_id,action_code,idempotency_sha256)
);
CREATE TRIGGER trg_migration_actions_update_prd131
BEFORE UPDATE ON migration_action_commands
BEGIN SELECT RAISE(ABORT,'migration_action_immutable'); END;
CREATE TRIGGER trg_migration_actions_delete_prd131
BEFORE DELETE ON migration_action_commands
BEGIN SELECT RAISE(ABORT,'migration_action_immutable'); END;

CREATE TABLE migration_support_access_audits(
  migration_support_access_audit_id TEXT PRIMARY KEY CHECK(
    length(migration_support_access_audit_id)=36
    AND substr(migration_support_access_audit_id,1,4)='msa_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  migration_assessment_id TEXT NOT NULL,
  migration_run_id TEXT,
  support_session_id TEXT NOT NULL,
  support_grant_id TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  operator_revision INTEGER NOT NULL CHECK(operator_revision>=1),
  field_scope TEXT NOT NULL CHECK(field_scope='difference_summary'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  accessed_at TEXT NOT NULL CHECK(unixepoch(accessed_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,migration_assessment_id)
    REFERENCES migration_assessments(workspace_id,tenant_id,migration_assessment_id),
  FOREIGN KEY(workspace_id,tenant_id,migration_run_id)
    REFERENCES migration_runs(workspace_id,tenant_id,migration_run_id),
  FOREIGN KEY(support_session_id) REFERENCES support_sessions(support_session_id),
  FOREIGN KEY(support_grant_id) REFERENCES support_grants(grant_id),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id)
);
CREATE TRIGGER trg_migration_support_audits_update_prd131
BEFORE UPDATE ON migration_support_access_audits
BEGIN SELECT RAISE(ABORT,'migration_support_audit_immutable'); END;
CREATE TRIGGER trg_migration_support_audits_delete_prd131
BEFORE DELETE ON migration_support_access_audits
BEGIN SELECT RAISE(ABORT,'migration_support_audit_immutable'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd131(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-131'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd131_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd131_manifest_sources VALUES
  ('migration_assessments','archive_then_delete','audit'),
  ('migration_plans','archive_then_delete','audit'),
  ('migration_assessment_snapshots','archive_then_delete','audit'),
  ('migration_runs','archive_then_delete','audit'),
  ('migration_differences','archive_then_delete','audit'),
  ('migration_cutover_checkpoints','archive_then_delete','audit'),
  ('migration_rollback_plans','archive_then_delete','audit'),
  ('migration_cutover_heads','delete','operational'),
  ('migration_events','archive_then_delete','audit'),
  ('migration_event_outbox','delete','operational'),
  ('migration_support_access_audits','archive_then_delete','audit');
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
JOIN prd131_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd131(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-131',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd131_manifest_sources ORDER BY source_table;
DROP TABLE prd131_manifest_sources;
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
CREATE TRIGGER trg_tenant_deletion_manifest_prd131_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd131
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd131_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd131
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd131_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd131
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd131_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd131_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_migration_permission_extensions)=2
      AND (SELECT COUNT(*) FROM workspace_migration_role_permissions)=5
      AND (SELECT COUNT(*) FROM migration_source_adapters)=7
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd131)=11
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd131_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
