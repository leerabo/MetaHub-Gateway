-- PRD-147 durable console-domain migration, route retirement, and scheduler registry.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd147_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd147_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0138' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_tenant_bindings','platform_operators','platform_operator_role_bindings',
      'workspace_navigation_contract_snapshots','workspace_navigation_contract_heads',
      'channel_migration_cursors','platform_customer_success_migration_cursors',
      'platform_growth_migration_cursors','brand_migration_cursors',
      'provider_engineering_scheduler_state','commercial_accounting_migration_cursors',
      'console_experience_migration_cursors','workspace_navigation_migration_cursors'))=13
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd147_up_guard;

CREATE TABLE platform_console_migration_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.console_migration.read','platform.console_migration.backfill',
    'platform.console_migration.approve','platform.console_migration.security_review',
    'platform.console_migration.execute')),
  introduced_in_migration TEXT NOT NULL CHECK(introduced_in_migration='0139')
);
INSERT INTO platform_console_migration_permission_extensions VALUES
  ('platform.console_migration.read','0139'),
  ('platform.console_migration.backfill','0139'),
  ('platform.console_migration.approve','0139'),
  ('platform.console_migration.security_review','0139'),
  ('platform.console_migration.execute','0139');

CREATE TABLE platform_console_migration_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'platform_owner','platform_ops','platform_security')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_console_migration_permission_extensions(permission_code)
);
INSERT INTO platform_console_migration_role_permissions VALUES
  ('platform_owner','platform.console_migration.read'),
  ('platform_owner','platform.console_migration.approve'),
  ('platform_ops','platform.console_migration.read'),
  ('platform_ops','platform.console_migration.backfill'),
  ('platform_ops','platform.console_migration.execute'),
  ('platform_security','platform.console_migration.read'),
  ('platform_security','platform.console_migration.security_review');

CREATE TRIGGER trg_platform_console_migration_permission_insert_prd147
BEFORE INSERT ON platform_console_migration_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_console_migration_permissions_immutable'); END;
CREATE TRIGGER trg_platform_console_migration_permission_update_prd147
BEFORE UPDATE ON platform_console_migration_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_console_migration_permissions_immutable'); END;
CREATE TRIGGER trg_platform_console_migration_permission_delete_prd147
BEFORE DELETE ON platform_console_migration_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_console_migration_permissions_immutable'); END;
CREATE TRIGGER trg_platform_console_migration_role_insert_prd147
BEFORE INSERT ON platform_console_migration_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_console_migration_roles_immutable'); END;
CREATE TRIGGER trg_platform_console_migration_role_update_prd147
BEFORE UPDATE ON platform_console_migration_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_console_migration_roles_immutable'); END;
CREATE TRIGGER trg_platform_console_migration_role_delete_prd147
BEFORE DELETE ON platform_console_migration_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_console_migration_roles_immutable'); END;

CREATE TABLE console_domain_migrations(
  console_domain_migration_id TEXT PRIMARY KEY CHECK(
    length(console_domain_migration_id)=36
    AND substr(console_domain_migration_id,1,4)='cdm_'
    AND substr(console_domain_migration_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  domain_code TEXT NOT NULL CHECK(domain_code IN(
    'channel_attribution','customer_success','growth_analytics','brand_distribution',
    'provider_engineering','commercial_accounting','console_experience',
    'workspace_navigation')),
  operation_generation INTEGER NOT NULL CHECK(operation_generation>=1),
  source_migration_version TEXT NOT NULL CHECK(source_migration_version GLOB '01[3][1-8]'),
  target_migration_version TEXT NOT NULL CHECK(target_migration_version=source_migration_version),
  source_inventory_revision INTEGER NOT NULL CHECK(source_inventory_revision>=1),
  target_generation INTEGER NOT NULL CHECK(target_generation>=0),
  contract_epoch INTEGER NOT NULL CHECK(contract_epoch>=1),
  state TEXT NOT NULL CHECK(state IN(
    'inventoried','backfill_queued','backfilling','shadowing','ready','quiescing',
    'cutting_over','validating','active','rollback_queued','rolling_back',
    'rolled_back','blocked')),
  progress_basis_points INTEGER NOT NULL CHECK(progress_basis_points BETWEEN 0 AND 10000),
  reconciliation_id TEXT,
  reconciliation_digest TEXT CHECK(reconciliation_digest IS NULL OR(
    length(reconciliation_digest)=64 AND reconciliation_digest NOT GLOB '*[^0-9a-f]*')),
  route_manifest_revision INTEGER NOT NULL CHECK(route_manifest_revision>=1),
  scheduler_manifest_revision INTEGER NOT NULL CHECK(scheduler_manifest_revision>=1),
  approved_generation INTEGER CHECK(approved_generation IS NULL OR approved_generation>=1),
  reviewed_generation INTEGER CHECK(reviewed_generation IS NULL OR reviewed_generation>=1),
  last_error_code TEXT CHECK(last_error_code IS NULL OR(
    length(last_error_code) BETWEEN 3 AND 80 AND last_error_code=lower(last_error_code)
    AND last_error_code NOT GLOB '*[^a-z0-9_.-]*')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  UNIQUE(workspace_id,tenant_id,domain_code,operation_generation),
  UNIQUE(workspace_id,tenant_id,console_domain_migration_id),
  UNIQUE(workspace_id,tenant_id,console_domain_migration_id,operation_generation),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_console_domain_migrations_prd147
  ON console_domain_migrations(workspace_id,tenant_id,state,domain_code,updated_at);

CREATE TABLE console_domain_migration_heads(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  domain_code TEXT NOT NULL,
  console_domain_migration_id TEXT NOT NULL UNIQUE,
  operation_generation INTEGER NOT NULL CHECK(operation_generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,tenant_id,domain_code),
  FOREIGN KEY(workspace_id,tenant_id,console_domain_migration_id,operation_generation)
    REFERENCES console_domain_migrations(
      workspace_id,tenant_id,console_domain_migration_id,operation_generation)
);

CREATE TABLE platform_console_domain_migrations(
  platform_console_domain_migration_id TEXT PRIMARY KEY CHECK(
    length(platform_console_domain_migration_id)=36
    AND substr(platform_console_domain_migration_id,1,4)='pcd_'
    AND substr(platform_console_domain_migration_id,5) NOT GLOB '*[^0-9a-f]*'),
  domain_code TEXT NOT NULL,
  operation_generation INTEGER NOT NULL CHECK(operation_generation>=1),
  state TEXT NOT NULL CHECK(state IN(
    'inventoried','backfill_queued','backfilling','shadowing','ready','quiescing',
    'cutting_over','validating','active','rollback_queued','rolling_back',
    'rolled_back','blocked')),
  workspace_total INTEGER NOT NULL CHECK(workspace_total>=0),
  workspace_ready INTEGER NOT NULL CHECK(workspace_ready BETWEEN 0 AND workspace_total),
  workspace_blocked INTEGER NOT NULL CHECK(workspace_blocked BETWEEN 0 AND workspace_total),
  manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(domain_code,operation_generation)
);

CREATE TABLE console_backfill_cursors(
  console_backfill_cursor_id TEXT PRIMARY KEY CHECK(
    length(console_backfill_cursor_id)=36
    AND substr(console_backfill_cursor_id,1,4)='cbf_'
    AND substr(console_backfill_cursor_id,5) NOT GLOB '*[^0-9a-f]*'),
  console_domain_migration_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  domain_code TEXT NOT NULL,
  mapping_code TEXT NOT NULL CHECK(length(mapping_code) BETWEEN 3 AND 80),
  source_table_code TEXT NOT NULL CHECK(length(source_table_code) BETWEEN 3 AND 80),
  source_schema_version TEXT NOT NULL CHECK(length(source_schema_version) BETWEEN 1 AND 40),
  target_table_code TEXT NOT NULL CHECK(length(target_table_code) BETWEEN 3 AND 80),
  target_schema_version TEXT NOT NULL CHECK(length(target_schema_version) BETWEEN 1 AND 40),
  source_upper_watermark_hmac TEXT NOT NULL CHECK(length(source_upper_watermark_hmac)=64),
  last_source_key_hmac TEXT CHECK(last_source_key_hmac IS NULL OR length(last_source_key_hmac)=64),
  source_row_count INTEGER NOT NULL CHECK(source_row_count>=0),
  target_row_count INTEGER NOT NULL CHECK(target_row_count>=0),
  source_digest_hmac TEXT NOT NULL CHECK(length(source_digest_hmac)=64),
  target_digest_hmac TEXT NOT NULL CHECK(length(target_digest_hmac)=64),
  digest_key_version INTEGER NOT NULL CHECK(digest_key_version>=1),
  owner_coverage_basis_points INTEGER NOT NULL CHECK(owner_coverage_basis_points BETWEEN 0 AND 10000),
  foreign_key_coverage_basis_points INTEGER NOT NULL CHECK(foreign_key_coverage_basis_points BETWEEN 0 AND 10000),
  event_watermark INTEGER NOT NULL CHECK(event_watermark>=0),
  outbox_watermark INTEGER NOT NULL CHECK(outbox_watermark>=0),
  audit_watermark INTEGER NOT NULL CHECK(audit_watermark>=0),
  scheduler_watermark INTEGER NOT NULL CHECK(scheduler_watermark>=0),
  attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
  status TEXT NOT NULL CHECK(status IN(
    'prepared','queued','running','paused','completed','cancelled','blocked')),
  fence_generation INTEGER NOT NULL CHECK(fence_generation>=0),
  lease_token_hmac TEXT CHECK(lease_token_hmac IS NULL OR length(lease_token_hmac)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(workspace_id,tenant_id,console_domain_migration_id,mapping_code),
  FOREIGN KEY(workspace_id,tenant_id,console_domain_migration_id)
    REFERENCES console_domain_migrations(workspace_id,tenant_id,console_domain_migration_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);

CREATE TABLE console_backfill_checkpoints(
  console_backfill_checkpoint_id TEXT PRIMARY KEY CHECK(
    length(console_backfill_checkpoint_id)=36
    AND substr(console_backfill_checkpoint_id,1,4)='cbc_'),
  console_backfill_cursor_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  sequence_number INTEGER NOT NULL CHECK(sequence_number>=1),
  source_key_hmac TEXT NOT NULL CHECK(length(source_key_hmac)=64),
  row_count INTEGER NOT NULL CHECK(row_count>=0),
  byte_count INTEGER NOT NULL CHECK(byte_count>=0),
  fence_generation INTEGER NOT NULL CHECK(fence_generation>=1),
  committed_at TEXT NOT NULL CHECK(unixepoch(committed_at) IS NOT NULL),
  UNIQUE(console_backfill_cursor_id,sequence_number),
  FOREIGN KEY(console_backfill_cursor_id)
    REFERENCES console_backfill_cursors(console_backfill_cursor_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);

CREATE TABLE console_backfill_attempts(
  console_backfill_attempt_id TEXT PRIMARY KEY CHECK(
    length(console_backfill_attempt_id)=36
    AND substr(console_backfill_attempt_id,1,4)='cba_'),
  console_backfill_cursor_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  attempt_number INTEGER NOT NULL CHECK(attempt_number>=1),
  outcome_code TEXT NOT NULL CHECK(length(outcome_code) BETWEEN 3 AND 80),
  started_at TEXT NOT NULL CHECK(unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  UNIQUE(console_backfill_cursor_id,attempt_number),
  FOREIGN KEY(console_backfill_cursor_id)
    REFERENCES console_backfill_cursors(console_backfill_cursor_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);

CREATE TABLE workspace_domain_reconciliations(
  domain_reconciliation_id TEXT PRIMARY KEY CHECK(
    length(domain_reconciliation_id)=36
    AND substr(domain_reconciliation_id,1,4)='drc_'
    AND substr(domain_reconciliation_id,5) NOT GLOB '*[^0-9a-f]*'),
  console_domain_migration_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  domain_code TEXT NOT NULL,
  operation_generation INTEGER NOT NULL CHECK(operation_generation>=1),
  source_watermark_hmac TEXT NOT NULL CHECK(length(source_watermark_hmac)=64),
  target_watermark_hmac TEXT NOT NULL CHECK(length(target_watermark_hmac)=64),
  contract_release_generation INTEGER NOT NULL CHECK(contract_release_generation>=0),
  check_profile TEXT NOT NULL CHECK(check_profile='console-domain-cutover-v1'),
  check_profile_version INTEGER NOT NULL CHECK(check_profile_version=1),
  status TEXT NOT NULL CHECK(status IN(
    'pending','matched','mismatched','unavailable','superseded')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80),
  expected_count INTEGER NOT NULL CHECK(expected_count>=0),
  observed_count INTEGER NOT NULL CHECK(observed_count>=0),
  expected_digest_hmac TEXT NOT NULL CHECK(length(expected_digest_hmac)=64),
  observed_digest_hmac TEXT NOT NULL CHECK(length(observed_digest_hmac)=64),
  coverage_basis_points INTEGER NOT NULL CHECK(coverage_basis_points BETWEEN 0 AND 10000),
  revision INTEGER NOT NULL CHECK(revision>=1),
  checked_at TEXT NOT NULL CHECK(unixepoch(checked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(workspace_id,tenant_id,domain_code,operation_generation),
  UNIQUE(workspace_id,tenant_id,domain_reconciliation_id,operation_generation),
  FOREIGN KEY(workspace_id,tenant_id,console_domain_migration_id,operation_generation)
    REFERENCES console_domain_migrations(
      workspace_id,tenant_id,console_domain_migration_id,operation_generation),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);

CREATE TABLE workspace_domain_reconciliation_checks(
  domain_reconciliation_check_id TEXT PRIMARY KEY CHECK(
    length(domain_reconciliation_check_id)=36
    AND substr(domain_reconciliation_check_id,1,4)='dck_'),
  domain_reconciliation_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  operation_generation INTEGER NOT NULL CHECK(operation_generation>=1),
  check_type TEXT NOT NULL CHECK(check_type IN(
    'source_target_rows','canonical_digest','owner_coverage','foreign_keys',
    'event_outbox','audit_watermark','scheduler_watermark','legacy_write_fence',
    'route_registration_zero','openapi_zero','frontend_runtime_zero','prefetch_zero',
    'permission_retirement','navigation_64_plus_4','cross_domain_negative',
    'target_smoke','n_minus_one_fenced','late_writer_fenced','static_sensitive_zero')),
  status TEXT NOT NULL CHECK(status IN('matched','mismatched','unavailable')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80),
  expected_count INTEGER NOT NULL CHECK(expected_count>=0),
  observed_count INTEGER NOT NULL CHECK(observed_count>=0),
  artifact_kind TEXT NOT NULL CHECK(length(artifact_kind) BETWEEN 3 AND 40),
  artifact_digest TEXT NOT NULL CHECK(length(artifact_digest)=64),
  tool_version TEXT NOT NULL CHECK(length(tool_version) BETWEEN 1 AND 40),
  retention_class TEXT NOT NULL CHECK(retention_class IN('release','security','operations')),
  checked_at TEXT NOT NULL CHECK(unixepoch(checked_at) IS NOT NULL),
  UNIQUE(domain_reconciliation_id,check_type),
  FOREIGN KEY(workspace_id,tenant_id,domain_reconciliation_id,operation_generation)
    REFERENCES workspace_domain_reconciliations(
      workspace_id,tenant_id,domain_reconciliation_id,operation_generation),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);

CREATE TABLE platform_domain_reconciliations(
  platform_domain_reconciliation_id TEXT PRIMARY KEY CHECK(
    length(platform_domain_reconciliation_id)=36
    AND substr(platform_domain_reconciliation_id,1,4)='pdr_'),
  domain_code TEXT NOT NULL,
  operation_generation INTEGER NOT NULL CHECK(operation_generation>=1),
  status TEXT NOT NULL CHECK(status IN(
    'pending','matched','mismatched','unavailable','superseded')),
  expected_count INTEGER NOT NULL CHECK(expected_count>=0),
  observed_count INTEGER NOT NULL CHECK(observed_count>=0),
  reconciliation_digest TEXT NOT NULL CHECK(length(reconciliation_digest)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  checked_at TEXT NOT NULL CHECK(unixepoch(checked_at) IS NOT NULL),
  UNIQUE(domain_code,operation_generation)
);

CREATE TABLE platform_domain_reconciliation_checks(
  platform_domain_reconciliation_check_id TEXT PRIMARY KEY CHECK(
    length(platform_domain_reconciliation_check_id)=36
    AND substr(platform_domain_reconciliation_check_id,1,4)='pdc_'),
  platform_domain_reconciliation_id TEXT NOT NULL,
  check_type TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('matched','mismatched','unavailable')),
  reason_code TEXT NOT NULL,
  expected_count INTEGER NOT NULL CHECK(expected_count>=0),
  observed_count INTEGER NOT NULL CHECK(observed_count>=0),
  artifact_digest TEXT NOT NULL CHECK(length(artifact_digest)=64),
  checked_at TEXT NOT NULL CHECK(unixepoch(checked_at) IS NOT NULL),
  UNIQUE(platform_domain_reconciliation_id,check_type),
  FOREIGN KEY(platform_domain_reconciliation_id)
    REFERENCES platform_domain_reconciliations(platform_domain_reconciliation_id)
);

CREATE TABLE console_contract_releases(
  console_contract_release_id TEXT PRIMARY KEY CHECK(
    length(console_contract_release_id)=36
    AND substr(console_contract_release_id,1,4)='ccr_'),
  generation INTEGER NOT NULL UNIQUE CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN('candidate','released','superseded','rejected')),
  target_generation_digest TEXT NOT NULL CHECK(length(target_generation_digest)=64),
  navigation_contract_digest TEXT NOT NULL CHECK(length(navigation_contract_digest)=64),
  route_manifest_digest TEXT NOT NULL CHECK(length(route_manifest_digest)=64),
  api_manifest_digest TEXT NOT NULL CHECK(length(api_manifest_digest)=64),
  permission_manifest_digest TEXT NOT NULL CHECK(length(permission_manifest_digest)=64),
  frontend_manifest_digest TEXT NOT NULL CHECK(length(frontend_manifest_digest)=64),
  telemetry_manifest_digest TEXT NOT NULL CHECK(length(telemetry_manifest_digest)=64),
  scheduler_manifest_digest TEXT NOT NULL CHECK(length(scheduler_manifest_digest)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  UNIQUE(console_contract_release_id,generation)
);

CREATE TABLE console_contract_release_items(
  console_contract_release_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  item_code TEXT NOT NULL CHECK(length(item_code) BETWEEN 3 AND 120),
  item_kind TEXT NOT NULL CHECK(item_kind IN(
    'domain','navigation','route','api','permission','frontend','telemetry','scheduler')),
  item_generation INTEGER NOT NULL CHECK(item_generation>=0),
  item_digest TEXT NOT NULL CHECK(length(item_digest)=64),
  PRIMARY KEY(console_contract_release_id,item_code),
  FOREIGN KEY(console_contract_release_id,generation)
    REFERENCES console_contract_releases(console_contract_release_id,generation)
);

CREATE TABLE console_contract_release_heads(
  head_key TEXT PRIMARY KEY CHECK(head_key='production'),
  console_contract_release_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL UNIQUE CHECK(generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(console_contract_release_id,generation)
    REFERENCES console_contract_releases(console_contract_release_id,generation)
);

CREATE TABLE legacy_route_catalog(
  legacy_route_id TEXT PRIMARY KEY CHECK(
    length(legacy_route_id)=36 AND substr(legacy_route_id,1,4)='lrt_'),
  route_code TEXT NOT NULL UNIQUE CHECK(length(route_code) BETWEEN 3 AND 120),
  method TEXT NOT NULL CHECK(method IN('GET','POST','PATCH','PUT','DELETE')),
  normalized_path TEXT NOT NULL CHECK(
    length(normalized_path) BETWEEN 2 AND 180 AND substr(normalized_path,1,1)='/'),
  source_prd TEXT NOT NULL CHECK(source_prd GLOB 'PRD-[0-9][0-9][0-9]'),
  owner_domain TEXT NOT NULL CHECK(owner_domain IN('platform','workspace','channel','global_account')),
  replacement_code TEXT CHECK(replacement_code IS NULL OR length(replacement_code) BETWEEN 3 AND 120),
  permission_code TEXT CHECK(permission_code IS NULL OR length(permission_code) BETWEEN 3 AND 120),
  retire_contract_epoch INTEGER NOT NULL CHECK(retire_contract_epoch=1),
  production_manifest_digest TEXT NOT NULL CHECK(length(production_manifest_digest)=64),
  status TEXT NOT NULL CHECK(status='planned'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(method,normalized_path)
);

CREATE TABLE workspace_legacy_route_tombstones(
  workspace_legacy_route_tombstone_id TEXT PRIMARY KEY CHECK(
    length(workspace_legacy_route_tombstone_id)=36
    AND substr(workspace_legacy_route_tombstone_id,1,4)='wlt_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  legacy_route_id TEXT NOT NULL,
  contract_generation INTEGER NOT NULL CHECK(contract_generation>=1),
  status TEXT NOT NULL CHECK(status IN(
    'planned','write_fenced','router_removed','verified')),
  zero_check_digest TEXT CHECK(zero_check_digest IS NULL OR length(zero_check_digest)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(workspace_id,tenant_id,legacy_route_id,contract_generation),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(legacy_route_id) REFERENCES legacy_route_catalog(legacy_route_id)
);

CREATE TABLE platform_legacy_route_retirements(
  platform_legacy_route_retirement_id TEXT PRIMARY KEY CHECK(
    length(platform_legacy_route_retirement_id)=36
    AND substr(platform_legacy_route_retirement_id,1,4)='plt_'),
  legacy_route_id TEXT NOT NULL,
  contract_generation INTEGER NOT NULL CHECK(contract_generation>=1),
  status TEXT NOT NULL CHECK(status IN(
    'planned','write_fenced','router_removed','verified')),
  zero_check_digest TEXT CHECK(zero_check_digest IS NULL OR length(zero_check_digest)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(legacy_route_id,contract_generation),
  FOREIGN KEY(legacy_route_id) REFERENCES legacy_route_catalog(legacy_route_id)
);

CREATE TABLE platform_console_migration_batches(
  platform_console_migration_batch_id TEXT PRIMARY KEY CHECK(
    length(platform_console_migration_batch_id)=36
    AND substr(platform_console_migration_batch_id,1,4)='cmb_'),
  domain_code TEXT NOT NULL,
  scope_code TEXT NOT NULL CHECK(scope_code IN('workspace','all_active')),
  workspace_ref TEXT CHECK(workspace_ref IS NULL OR length(workspace_ref)=64),
  status TEXT NOT NULL CHECK(status IN(
    'prepared','queued','running','paused','completed','cancelled','blocked')),
  workspace_total INTEGER NOT NULL CHECK(workspace_total>=0),
  workspace_completed INTEGER NOT NULL CHECK(workspace_completed BETWEEN 0 AND workspace_total),
  workspace_blocked INTEGER NOT NULL CHECK(workspace_blocked BETWEEN 0 AND workspace_total),
  operation_generation INTEGER NOT NULL CHECK(operation_generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);

CREATE TABLE platform_console_migration_batch_items(
  platform_console_migration_batch_item_id TEXT PRIMARY KEY CHECK(
    length(platform_console_migration_batch_item_id)=36
    AND substr(platform_console_migration_batch_item_id,1,4)='cbi_'
    AND substr(platform_console_migration_batch_item_id,5) NOT GLOB '*[^0-9a-f]*'),
  platform_console_migration_batch_id TEXT NOT NULL,
  console_domain_migration_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  operation_generation INTEGER NOT NULL CHECK(operation_generation>=1),
  sequence_number INTEGER NOT NULL CHECK(sequence_number>=1),
  status TEXT NOT NULL CHECK(status IN(
    'inventoried','queued','running','completed','blocked','cancelled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(platform_console_migration_batch_id,sequence_number),
  UNIQUE(platform_console_migration_batch_id,workspace_id,tenant_id),
  UNIQUE(platform_console_migration_batch_id,console_domain_migration_id),
  FOREIGN KEY(platform_console_migration_batch_id)
    REFERENCES platform_console_migration_batches(platform_console_migration_batch_id),
  FOREIGN KEY(workspace_id,tenant_id,console_domain_migration_id,operation_generation)
    REFERENCES console_domain_migrations(
      workspace_id,tenant_id,console_domain_migration_id,operation_generation),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_platform_console_migration_batch_items_prd147
  ON platform_console_migration_batch_items(
    platform_console_migration_batch_id,status,sequence_number);

CREATE TABLE platform_console_migration_commands(
  platform_console_migration_command_id TEXT PRIMARY KEY CHECK(
    length(platform_console_migration_command_id)=36
    AND substr(platform_console_migration_command_id,1,4)='cmc_'),
  operator_id TEXT NOT NULL,
  operator_revision INTEGER NOT NULL CHECK(operator_revision>=1),
  platform_session_id TEXT NOT NULL,
  global_session_id TEXT NOT NULL,
  factor_id TEXT NOT NULL,
  factor_revision INTEGER NOT NULL CHECK(factor_revision>=1),
  contract_generation INTEGER NOT NULL CHECK(contract_generation>=0),
  registry_generation INTEGER NOT NULL CHECK(registry_generation>=1),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'backfill.prepare','backfill.start','backfill.resume','backfill.cancel',
    'cutover.prepare','cutover.approve','cutover.security_review','cutover.execute',
    'rollback.prepare','rollback.approve','rollback.security_review',
    'rollback.execute','rollback.resume')),
  console_domain_migration_id TEXT,
  platform_console_migration_batch_id TEXT,
  workspace_ref TEXT CHECK(workspace_ref IS NULL OR length(workspace_ref)=64),
  expected_revision INTEGER NOT NULL CHECK(expected_revision>=0),
  idempotency_key_hmac TEXT NOT NULL CHECK(length(idempotency_key_hmac)=64),
  request_digest_hmac TEXT NOT NULL CHECK(length(request_digest_hmac)=64),
  permission_fingerprint TEXT NOT NULL CHECK(length(permission_fingerprint)=64),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  result_state TEXT NOT NULL CHECK(length(result_state) BETWEEN 3 AND 40),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(operator_id,action_code,idempotency_key_hmac),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(platform_session_id) REFERENCES platform_sessions(platform_session_id),
  FOREIGN KEY(global_session_id) REFERENCES global_sessions(session_id),
  FOREIGN KEY(factor_id) REFERENCES account_mfa_factors(factor_id),
  FOREIGN KEY(console_domain_migration_id)
    REFERENCES console_domain_migrations(console_domain_migration_id),
  FOREIGN KEY(platform_console_migration_batch_id)
    REFERENCES platform_console_migration_batches(platform_console_migration_batch_id)
);

CREATE TABLE platform_console_migration_approvals(
  platform_console_migration_approval_id TEXT PRIMARY KEY CHECK(
    length(platform_console_migration_approval_id)=36
    AND substr(platform_console_migration_approval_id,1,4)='cma_'),
  console_domain_migration_id TEXT NOT NULL,
  action_kind TEXT NOT NULL CHECK(action_kind IN('cutover','rollback')),
  operation_generation INTEGER NOT NULL CHECK(operation_generation>=1),
  prepare_operator_id TEXT NOT NULL,
  approver_operator_id TEXT NOT NULL,
  approver_operator_revision INTEGER NOT NULL CHECK(approver_operator_revision>=1),
  approver_platform_session_id TEXT NOT NULL,
  approver_global_session_id TEXT NOT NULL,
  approver_factor_id TEXT NOT NULL,
  approver_factor_revision INTEGER NOT NULL CHECK(approver_factor_revision>=1),
  contract_generation INTEGER NOT NULL CHECK(contract_generation>=0),
  registry_generation INTEGER NOT NULL CHECK(registry_generation>=1),
  migration_revision INTEGER NOT NULL CHECK(migration_revision>=1),
  reconciliation_digest TEXT NOT NULL CHECK(length(reconciliation_digest)=64),
  route_manifest_digest TEXT NOT NULL CHECK(length(route_manifest_digest)=64),
  scheduler_manifest_digest TEXT NOT NULL CHECK(length(scheduler_manifest_digest)=64),
  permission_fingerprint TEXT NOT NULL CHECK(length(permission_fingerprint)=64),
  status TEXT NOT NULL CHECK(status IN('active','consumed','expired','revoked')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  UNIQUE(console_domain_migration_id,action_kind,operation_generation),
  CHECK(prepare_operator_id!=approver_operator_id),
  FOREIGN KEY(console_domain_migration_id)
    REFERENCES console_domain_migrations(console_domain_migration_id),
  FOREIGN KEY(prepare_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(approver_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(approver_platform_session_id) REFERENCES platform_sessions(platform_session_id),
  FOREIGN KEY(approver_global_session_id) REFERENCES global_sessions(session_id),
  FOREIGN KEY(approver_factor_id) REFERENCES account_mfa_factors(factor_id)
);

CREATE TABLE platform_console_migration_security_reviews(
  platform_console_migration_security_review_id TEXT PRIMARY KEY CHECK(
    length(platform_console_migration_security_review_id)=36
    AND substr(platform_console_migration_security_review_id,1,4)='cms_'),
  console_domain_migration_id TEXT NOT NULL,
  action_kind TEXT NOT NULL CHECK(action_kind IN('cutover','rollback')),
  operation_generation INTEGER NOT NULL CHECK(operation_generation>=1),
  prepare_operator_id TEXT NOT NULL,
  reviewer_operator_id TEXT NOT NULL,
  reviewer_operator_revision INTEGER NOT NULL CHECK(reviewer_operator_revision>=1),
  reviewer_platform_session_id TEXT NOT NULL,
  reviewer_global_session_id TEXT NOT NULL,
  reviewer_factor_id TEXT NOT NULL,
  reviewer_factor_revision INTEGER NOT NULL CHECK(reviewer_factor_revision>=1),
  contract_generation INTEGER NOT NULL CHECK(contract_generation>=0),
  registry_generation INTEGER NOT NULL CHECK(registry_generation>=1),
  reconciliation_id TEXT NOT NULL,
  reconciliation_revision INTEGER NOT NULL CHECK(reconciliation_revision>=1),
  check_profile TEXT NOT NULL CHECK(check_profile='console-domain-negative-v1'),
  profile_version INTEGER NOT NULL CHECK(profile_version=1),
  mandatory_case_count INTEGER NOT NULL CHECK(mandatory_case_count>=1),
  artifact_digest TEXT NOT NULL CHECK(length(artifact_digest)=64),
  tool_version TEXT NOT NULL CHECK(length(tool_version) BETWEEN 1 AND 40),
  permission_fingerprint TEXT NOT NULL CHECK(length(permission_fingerprint)=64),
  status TEXT NOT NULL CHECK(status IN('active','consumed','expired','revoked')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  UNIQUE(console_domain_migration_id,action_kind,operation_generation),
  CHECK(prepare_operator_id!=reviewer_operator_id),
  FOREIGN KEY(console_domain_migration_id)
    REFERENCES console_domain_migrations(console_domain_migration_id),
  FOREIGN KEY(reviewer_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(reviewer_platform_session_id) REFERENCES platform_sessions(platform_session_id),
  FOREIGN KEY(reviewer_global_session_id) REFERENCES global_sessions(session_id),
  FOREIGN KEY(reviewer_factor_id) REFERENCES account_mfa_factors(factor_id)
);

CREATE TABLE platform_console_migration_events(
  platform_console_migration_event_id TEXT PRIMARY KEY CHECK(
    length(platform_console_migration_event_id)=36
    AND substr(platform_console_migration_event_id,1,4)='cme_'),
  workspace_id TEXT,
  tenant_id TEXT,
  aggregate_ref TEXT NOT NULL CHECK(length(aggregate_ref)=64),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'console_domain.backfilled','console_domain.cutover',
    'console_domain.rolled_back','legacy_route.retired')),
  schema_version INTEGER NOT NULL CHECK(schema_version=1),
  domain_code TEXT NOT NULL,
  operation_generation INTEGER NOT NULL CHECK(operation_generation>=1),
  contract_generation INTEGER NOT NULL CHECK(contract_generation>=0),
  reconciliation_digest TEXT NOT NULL CHECK(length(reconciliation_digest)=64),
  payload_json TEXT NOT NULL CHECK(
    json_valid(payload_json) AND json_type(payload_json)='object'
    AND json(payload_json)=payload_json),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  UNIQUE(aggregate_ref,event_type,operation_generation),
  CHECK((workspace_id IS NULL AND tenant_id IS NULL)
    OR(workspace_id IS NOT NULL AND tenant_id IS NOT NULL)),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);

CREATE TABLE platform_console_migration_event_outbox(
  platform_console_migration_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(platform_console_migration_event_outbox_id)=36
    AND substr(platform_console_migration_event_outbox_id,1,4)='cmo_'),
  platform_console_migration_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','processing','sent','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_generation INTEGER NOT NULL CHECK(lease_generation>=0),
  lease_token_hmac TEXT CHECK(lease_token_hmac IS NULL OR length(lease_token_hmac)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  dispatched_at TEXT CHECK(dispatched_at IS NULL OR unixepoch(dispatched_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(platform_console_migration_event_id)
    REFERENCES platform_console_migration_events(platform_console_migration_event_id)
);

CREATE TABLE platform_console_migration_audits(
  platform_console_migration_audit_id TEXT PRIMARY KEY CHECK(
    length(platform_console_migration_audit_id)=36
    AND substr(platform_console_migration_audit_id,1,4)='cau_'),
  operator_pseudonym TEXT NOT NULL CHECK(length(operator_pseudonym)=64),
  operator_revision INTEGER NOT NULL CHECK(operator_revision>=1),
  factor_revision INTEGER NOT NULL CHECK(factor_revision>=1),
  session_epoch_hmac TEXT NOT NULL CHECK(length(session_epoch_hmac)=64),
  action_code TEXT NOT NULL CHECK(length(action_code) BETWEEN 3 AND 80),
  owner_ref TEXT CHECK(owner_ref IS NULL OR length(owner_ref)=64),
  old_state TEXT,
  new_state TEXT,
  quoted_revision INTEGER NOT NULL CHECK(quoted_revision>=0),
  idempotency_key_hmac TEXT NOT NULL CHECK(length(idempotency_key_hmac)=64),
  permission_fingerprint TEXT NOT NULL CHECK(length(permission_fingerprint)=64),
  result_code TEXT NOT NULL CHECK(length(result_code) BETWEEN 3 AND 80),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);

CREATE TABLE scheduler_registry_entries(
  scheduler_registry_entry_id TEXT PRIMARY KEY CHECK(
    length(scheduler_registry_entry_id)=36
    AND substr(scheduler_registry_entry_id,1,4)='sre_'),
  scheduler_code TEXT NOT NULL UNIQUE CHECK(length(scheduler_code) BETWEEN 3 AND 80),
  execution_class TEXT NOT NULL CHECK(execution_class IN('cluster_singleton','cluster_partitioned')),
  owner_domain TEXT NOT NULL CHECK(owner_domain IN(
    'platform_global','workspace_composite','channel','deployment','global_account','fixed_split')),
  config_condition TEXT NOT NULL CHECK(config_condition IN(
    'always','gateway_placement_enabled','control_background_schedulers_enabled')),
  lease_policy_code TEXT NOT NULL CHECK(lease_policy_code='scheduler-registry-v1'),
  watermark_schema_version INTEGER NOT NULL CHECK(watermark_schema_version=1),
  manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64),
  enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),
  seed_entry INTEGER NOT NULL CHECK(seed_entry=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);

CREATE TABLE scheduler_registry_heads(
  head_key TEXT PRIMARY KEY CHECK(head_key='production'),
  manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64),
  generation INTEGER NOT NULL CHECK(generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);

CREATE TABLE scheduler_baseline_mappings(
  baseline_scheduler_code TEXT PRIMARY KEY CHECK(length(baseline_scheduler_code) BETWEEN 3 AND 80),
  final_scheduler_code TEXT NOT NULL,
  mapping_kind TEXT NOT NULL CHECK(mapping_kind IN('same','replacement','merged')),
  mapping_digest TEXT NOT NULL CHECK(length(mapping_digest)=64),
  FOREIGN KEY(final_scheduler_code) REFERENCES scheduler_registry_entries(scheduler_code)
);

CREATE TABLE scheduler_queue_contracts(
  scheduler_queue_contract_id TEXT PRIMARY KEY CHECK(
    length(scheduler_queue_contract_id)=36
    AND substr(scheduler_queue_contract_id,1,4)='sqc_'),
  scheduler_code TEXT NOT NULL,
  queue_code TEXT NOT NULL UNIQUE CHECK(length(queue_code) BETWEEN 3 AND 120),
  owner_domain TEXT NOT NULL CHECK(owner_domain IN(
    'platform_global','workspace_composite','channel','deployment','global_account','fixed_split')),
  partition_policy_code TEXT NOT NULL CHECK(partition_policy_code IN('global','owner_hash_v1')),
  manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64),
  enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),
  revision INTEGER NOT NULL CHECK(revision>=1),
  FOREIGN KEY(scheduler_code) REFERENCES scheduler_registry_entries(scheduler_code)
);

CREATE TABLE scheduler_leases(
  scheduler_code TEXT NOT NULL,
  partition_code TEXT NOT NULL CHECK(length(partition_code) BETWEEN 1 AND 120),
  lease_owner_pseudonym TEXT NOT NULL CHECK(length(lease_owner_pseudonym)=64),
  lease_deadline_at TEXT NOT NULL CHECK(unixepoch(lease_deadline_at) IS NOT NULL),
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=1),
  lease_token_hmac TEXT NOT NULL CHECK(length(lease_token_hmac)=64),
  last_started_at TEXT NOT NULL CHECK(unixepoch(last_started_at) IS NOT NULL),
  last_completed_at TEXT CHECK(last_completed_at IS NULL OR unixepoch(last_completed_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR length(last_error_code) BETWEEN 3 AND 80),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(scheduler_code,partition_code),
  FOREIGN KEY(scheduler_code) REFERENCES scheduler_registry_entries(scheduler_code)
);

CREATE TABLE scheduler_watermarks(
  scheduler_code TEXT NOT NULL,
  partition_code TEXT NOT NULL,
  source_code TEXT NOT NULL CHECK(length(source_code) BETWEEN 3 AND 120),
  checkpoint_hmac TEXT NOT NULL CHECK(length(checkpoint_hmac)=64),
  business_generation INTEGER NOT NULL CHECK(business_generation>=0),
  last_committed_fence INTEGER NOT NULL CHECK(last_committed_fence>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(scheduler_code,partition_code,source_code),
  FOREIGN KEY(scheduler_code) REFERENCES scheduler_registry_entries(scheduler_code)
);

CREATE TABLE runtime_surface_manifest(
  runtime_surface_code TEXT PRIMARY KEY CHECK(runtime_surface_code IN(
    'scheduler-lease-coordinator','risk-fact-sink','control-telemetry')),
  execution_class TEXT NOT NULL CHECK(execution_class IN(
    'per_replica_control_bounded','per_replica_bounded')),
  capacity_limit INTEGER NOT NULL CHECK(capacity_limit BETWEEN 1 AND 65536),
  batch_limit INTEGER NOT NULL CHECK(batch_limit BETWEEN 1 AND capacity_limit),
  manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64),
  revision INTEGER NOT NULL CHECK(revision=1)
);

CREATE TABLE runtime_surface_observations(
  runtime_surface_observation_id TEXT PRIMARY KEY CHECK(
    length(runtime_surface_observation_id)=36
    AND substr(runtime_surface_observation_id,1,4)='rso_'),
  runtime_surface_code TEXT NOT NULL,
  replica_pseudonym TEXT NOT NULL CHECK(length(replica_pseudonym)=64),
  accepted_count INTEGER NOT NULL CHECK(accepted_count>=0),
  dropped_count INTEGER NOT NULL CHECK(dropped_count>=0),
  persisted_count INTEGER NOT NULL CHECK(persisted_count>=0),
  backend_failure_count INTEGER NOT NULL CHECK(backend_failure_count>=0),
  queued_count INTEGER NOT NULL CHECK(queued_count BETWEEN 0 AND 65536),
  shutdown_timeout_count INTEGER NOT NULL CHECK(shutdown_timeout_count>=0),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  FOREIGN KEY(runtime_surface_code) REFERENCES runtime_surface_manifest(runtime_surface_code)
);

CREATE TABLE console_ingress_contract_heads(
  head_key TEXT PRIMARY KEY CHECK(head_key='production'),
  route_deny_epoch INTEGER NOT NULL CHECK(route_deny_epoch>=1),
  contract_generation INTEGER NOT NULL CHECK(contract_generation>=0),
  route_manifest_digest TEXT NOT NULL CHECK(length(route_manifest_digest)=64),
  legacy_write_fenced INTEGER NOT NULL CHECK(legacy_write_fenced IN(0,1)),
  status TEXT NOT NULL CHECK(status IN('expanded','active')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);

WITH manifest(scheduler_code,owner_domain,config_condition) AS (VALUES
  ('status-publication','platform_global','always'),
  ('gateway-snapshot','deployment','gateway_placement_enabled'),
  ('admin-assistant','workspace_composite','control_background_schedulers_enabled'),
  ('provider-health','workspace_composite','control_background_schedulers_enabled'),
  ('usage-export','workspace_composite','control_background_schedulers_enabled'),
  ('cost-anomaly','workspace_composite','control_background_schedulers_enabled'),
  ('platform-customer-success','platform_global','control_background_schedulers_enabled'),
  ('customer-support','fixed_split','control_background_schedulers_enabled'),
  ('developer-contract','platform_global','control_background_schedulers_enabled'),
  ('privacy','global_account','control_background_schedulers_enabled'),
  ('compliance','fixed_split','control_background_schedulers_enabled'),
  ('supply-chain','platform_global','control_background_schedulers_enabled'),
  ('security-assurance','fixed_split','control_background_schedulers_enabled'),
  ('launch-governance','platform_global','control_background_schedulers_enabled'),
  ('request-explanation','workspace_composite','control_background_schedulers_enabled'),
  ('request-log-query-event','workspace_composite','control_background_schedulers_enabled'),
  ('platform-provider-engineering','fixed_split','control_background_schedulers_enabled'),
  ('audit-export','workspace_composite','control_background_schedulers_enabled'),
  ('webhook-replay','workspace_composite','control_background_schedulers_enabled'),
  ('file-security-event','workspace_composite','control_background_schedulers_enabled'),
  ('experiment','workspace_composite','control_background_schedulers_enabled'),
  ('response-cache-outbox','workspace_composite','control_background_schedulers_enabled'),
  ('platform-growth','platform_global','control_background_schedulers_enabled'),
  ('approval','workspace_composite','control_background_schedulers_enabled'),
  ('commercial-accounting','fixed_split','control_background_schedulers_enabled'),
  ('notification','fixed_split','control_background_schedulers_enabled'),
  ('observability','fixed_split','control_background_schedulers_enabled'),
  ('risk','workspace_composite','control_background_schedulers_enabled'),
  ('channel-attribution','channel','control_background_schedulers_enabled'),
  ('brand-domain','fixed_split','control_background_schedulers_enabled'),
  ('security-event','workspace_composite','control_background_schedulers_enabled'),
  ('mcp','workspace_composite','control_background_schedulers_enabled'),
  ('tenant-data','workspace_composite','control_background_schedulers_enabled'),
  ('data-governance','workspace_composite','control_background_schedulers_enabled'),
  ('edge-support-bundle','deployment','control_background_schedulers_enabled'),
  ('console-experience','fixed_split','control_background_schedulers_enabled'),
  ('platform-job-runtime','platform_global','control_background_schedulers_enabled')
)
INSERT INTO scheduler_registry_entries(
  scheduler_registry_entry_id,scheduler_code,execution_class,owner_domain,
  config_condition,lease_policy_code,watermark_schema_version,manifest_digest,
  enabled,seed_entry,revision,created_at,updated_at)
SELECT 'sre_'||substr(metahub_sha256('prd147/scheduler/'||scheduler_code),1,32),
  scheduler_code,
  CASE WHEN owner_domain IN('workspace_composite','channel','deployment','global_account','fixed_split')
    THEN 'cluster_partitioned' ELSE 'cluster_singleton' END,
  owner_domain,config_condition,'scheduler-registry-v1',1,
  metahub_sha256(scheduler_code||'|'||owner_domain||'|'||config_condition||'|v1'),
  1,1,1,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM manifest;

WITH mapping(baseline_scheduler_code,final_scheduler_code,mapping_kind) AS (VALUES
  ('install_status_publication_scheduler','status-publication','same'),
  ('install_gateway_snapshot_scheduler','gateway-snapshot','same'),
  ('install_admin_assistant_scheduler','admin-assistant','same'),
  ('install_provider_health_scheduler','provider-health','same'),
  ('install_usage_export_scheduler','usage-export','same'),
  ('install_cost_anomaly_scheduler','cost-anomaly','same'),
  ('install_customer_health_scheduler','platform-customer-success','replacement'),
  ('install_customer_support_scheduler','customer-support','same'),
  ('install_developer_contract_scheduler','developer-contract','same'),
  ('install_privacy_scheduler','privacy','same'),
  ('install_compliance_scheduler','compliance','same'),
  ('install_supply_chain_scheduler','supply-chain','same'),
  ('install_security_assurance_scheduler','security-assurance','same'),
  ('install_launch_governance_scheduler','launch-governance','same'),
  ('install_request_explanation_scheduler','request-explanation','same'),
  ('install_request_log_query_event_scheduler','request-log-query-event','same'),
  ('install_provider_error_map_scheduler','platform-provider-engineering','merged'),
  ('install_audit_export_scheduler','audit-export','same'),
  ('install_webhook_replay_scheduler','webhook-replay','same'),
  ('install_file_security_event_scheduler','file-security-event','same'),
  ('install_adapter_contract_event_scheduler','platform-provider-engineering','merged'),
  ('install_experiment_scheduler','experiment','same'),
  ('install_response_cache_outbox_scheduler','response-cache-outbox','same'),
  ('install_product_event_scheduler','platform-growth','replacement'),
  ('install_approval_scheduler','approval','same'),
  ('install_plan_billing_scheduler','commercial-accounting','merged'),
  ('install_saas_billing_scheduler','commercial-accounting','merged'),
  ('install_platform_billing_scheduler','commercial-accounting','merged'),
  ('install_payment_commerce_scheduler','commercial-accounting','merged'),
  ('install_invoice_reconciliation_scheduler','commercial-accounting','merged'),
  ('install_notification_scheduler','notification','same'),
  ('install_observability_scheduler','observability','same'),
  ('install_risk_scheduler','risk','same'),
  ('install_partner_portal_scheduler','channel-attribution','replacement'),
  ('install_branding_scheduler','brand-domain','replacement'),
  ('install_billing_dispute_scheduler','commercial-accounting','merged'),
  ('install_security_event_scheduler','security-event','same'),
  ('install_mcp_scheduler','mcp','same'),
  ('install_tenant_data_scheduler','tenant-data','same'),
  ('install_data_governance_scheduler','data-governance','same'),
  ('install_edge_support_bundle_scheduler','edge-support-bundle','same')
)
INSERT INTO scheduler_baseline_mappings(
  baseline_scheduler_code,final_scheduler_code,mapping_kind,mapping_digest)
SELECT baseline_scheduler_code,final_scheduler_code,mapping_kind,
  metahub_sha256(baseline_scheduler_code||'|'||final_scheduler_code||'|'||mapping_kind)
FROM mapping;

INSERT INTO scheduler_queue_contracts(
  scheduler_queue_contract_id,scheduler_code,queue_code,owner_domain,
  partition_policy_code,manifest_digest,enabled,revision)
SELECT 'sqc_'||substr(metahub_sha256('prd147/queue/'||scheduler_code||'.main'),1,32),
  scheduler_code,scheduler_code||'.main',owner_domain,
  CASE WHEN execution_class='cluster_partitioned' THEN 'owner_hash_v1' ELSE 'global' END,
  metahub_sha256(scheduler_code||'.main|'||owner_domain||'|v1'),1,1
FROM scheduler_registry_entries;

WITH child(scheduler_code,queue_code,owner_domain,partition_policy_code) AS (VALUES
  ('platform-customer-success','platform-customer-success.health','platform_global','global'),
  ('platform-customer-success','platform-customer-success.plan','platform_global','global'),
  ('platform-customer-success','platform-customer-success.renewal','platform_global','global'),
  ('platform-provider-engineering','platform-provider-engineering.contract','platform_global','global'),
  ('platform-provider-engineering','platform-provider-engineering.error-catalog','platform_global','global'),
  ('platform-provider-engineering','platform-provider-engineering.compatibility','workspace_composite','owner_hash_v1'),
  ('commercial-accounting','commercial-accounting.plan','platform_global','global'),
  ('commercial-accounting','commercial-accounting.subscription','workspace_composite','owner_hash_v1'),
  ('commercial-accounting','commercial-accounting.payment','workspace_composite','owner_hash_v1'),
  ('commercial-accounting','commercial-accounting.invoice','workspace_composite','owner_hash_v1'),
  ('commercial-accounting','commercial-accounting.dispute','fixed_split','owner_hash_v1'),
  ('commercial-accounting','commercial-accounting.reconciliation','fixed_split','owner_hash_v1'),
  ('console-experience','console-experience.platform-dashboard','platform_global','global'),
  ('console-experience','console-experience.workspace-dashboard','workspace_composite','owner_hash_v1'),
  ('console-experience','console-experience.platform-approval','platform_global','global'),
  ('console-experience','console-experience.workspace-approval','workspace_composite','owner_hash_v1'),
  ('console-experience','console-experience.account-preference','global_account','owner_hash_v1'),
  ('console-experience','console-experience.deployment-diagnostic','deployment','owner_hash_v1'),
  ('console-experience','console-experience.workspace-navigation','workspace_composite','owner_hash_v1'),
  ('console-experience','console-experience.role-task-home','workspace_composite','owner_hash_v1'),
  ('console-experience','console-experience.advanced-tool','workspace_composite','owner_hash_v1'),
  ('console-experience','console-experience.navigation-outbox','workspace_composite','owner_hash_v1'),
  ('console-experience','console-experience.navigation-reconciliation','workspace_composite','owner_hash_v1'),
  ('platform-job-runtime','platform-job-runtime.jobs','platform_global','global'),
  ('channel-attribution','channel-attribution.statements','channel','owner_hash_v1'),
  ('brand-domain','brand-domain.verification','fixed_split','owner_hash_v1'),
  ('platform-growth','platform-growth.events','platform_global','global')
)
INSERT INTO scheduler_queue_contracts(
  scheduler_queue_contract_id,scheduler_code,queue_code,owner_domain,
  partition_policy_code,manifest_digest,enabled,revision)
SELECT 'sqc_'||substr(metahub_sha256('prd147/queue/'||queue_code),1,32),
  scheduler_code,queue_code,owner_domain,partition_policy_code,
  metahub_sha256(queue_code||'|'||owner_domain||'|v1'),1,1
FROM child;

INSERT INTO scheduler_registry_heads(
  head_key,manifest_digest,generation,revision,updated_at)
SELECT 'production',metahub_sha256(group_concat(scheduler_code||':'||manifest_digest,'|')),
  1,1,strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM (SELECT scheduler_code,manifest_digest FROM scheduler_registry_entries ORDER BY scheduler_code);

INSERT INTO runtime_surface_manifest VALUES
  ('scheduler-lease-coordinator','per_replica_control_bounded',64,16,
    metahub_sha256('scheduler-lease-coordinator|per_replica_control_bounded|64|16|v1'),1),
  ('risk-fact-sink','per_replica_bounded',4096,100,
    metahub_sha256('risk-fact-sink|per_replica_bounded|4096|100|v1'),1),
  ('control-telemetry','per_replica_bounded',4096,200,
    metahub_sha256('control-telemetry|per_replica_bounded|4096|200|v1'),1);

WITH route(
  route_code,method,normalized_path,source_prd,owner_domain,replacement_code,permission_code
) AS (VALUES
  ('partner-create','POST','/partners','PRD-087','channel','platform.channel_partner','partner.portal.configure'),
  ('partner-profile','GET','/partners/profile','PRD-087','channel','platform.channel_partner','partner.portal.read'),
  ('partner-branding','PATCH','/partners/branding','PRD-087','channel','channel.brand','partner.branding.manage'),
  ('partner-projects','GET','/partners/projects','PRD-087','channel','platform.channel_partner','partner.customer.manage'),
  ('partner-customers-list','GET','/partners/customers','PRD-087','channel','platform.channel_attribution','partner.portal.read'),
  ('partner-customers-create','POST','/partners/customers','PRD-087','channel','platform.channel_attribution','partner.customer.manage'),
  ('partner-customers-update','PATCH','/partners/customers/{partner_customer_id}','PRD-087','channel','platform.channel_attribution','partner.customer.manage'),
  ('partner-commission','GET','/partners/commission','PRD-087','channel','platform.channel_statement','partner.portal.read'),
  ('partner-delivery-package','GET','/partners/delivery-package','PRD-087','channel','platform.channel_delivery','partner.portal.read'),
  ('customer-health','GET','/customer-health','PRD-086','workspace','platform.customer_success','customer_health.read'),
  ('product-funnel','GET','/product-analytics/funnel','PRD-085','workspace','platform.growth','product.analytics.read'),
  ('tenant-brand-read','GET','/branding','PRD-088','workspace','workspace.brand','branding.read'),
  ('tenant-brand-write','PATCH','/branding','PRD-088','workspace','workspace.brand','branding.write'),
  ('tenant-brand-logo','GET','/branding/logo','PRD-088','workspace','workspace.brand','branding.read'),
  ('tenant-public-brand-logo','GET','/branding/public/logo','PRD-088','workspace','verified-host-brand','branding.read'),
  ('adapter-specs','GET','/adapter-specs','PRD-063','workspace','platform.provider_engineering','provider.health.read'),
  ('adapter-contract-runs','GET','/adapter-contract-runs','PRD-064','workspace','platform.provider_engineering','provider.health.read'),
  ('adapter-contract-run-detail','GET','/adapter-contract-runs/{adapter_contract_run_id}','PRD-064','workspace','platform.provider_engineering','provider.health.read'),
  ('error-maps','GET','/error-maps','PRD-056','workspace','workspace.error_map_override','provider.error_map.read'),
  ('error-map-create','POST','/error-maps','PRD-056','workspace','workspace.error_map_override','provider.error_map.write'),
  ('error-map-update','PATCH','/error-maps/{error_map_id}','PRD-056','workspace','workspace.error_map_override','provider.error_map.write'),
  ('tenant-plan-read','GET','/plans','PRD-079','workspace','workspace.subscription','billing.plan.read'),
  ('tenant-plan-update','PATCH','/plans','PRD-079','workspace','workspace.subscription','billing.plan.write'),
  ('model-prices-read','GET','/model-prices','PRD-051','workspace','platform.commercial_accounting','billing.price.read'),
  ('model-prices-create','POST','/model-prices','PRD-051','workspace','platform.commercial_accounting','billing.price.write'),
  ('billing-disputes-list','GET','/billing-disputes','PRD-080','workspace','workspace.billing_dispute','billing.dispute.read'),
  ('billing-dispute-detail','GET','/billing-disputes/{billing_dispute_id}','PRD-080','workspace','workspace.billing_dispute','billing.dispute.read'),
  ('billing-dispute-create','POST','/billing-disputes','PRD-080','workspace','workspace.billing_dispute','billing.dispute.create'),
  ('billing-dispute-update','PATCH','/billing-disputes/{billing_dispute_id}','PRD-080','workspace','workspace.billing_dispute','billing.dispute.write'),
  ('subscription-current','GET','/subscriptions/current','PRD-098','workspace','workspace.subscription','billing.subscription.read'),
  ('subscription-change-plan','POST','/subscriptions/current/change-plan','PRD-098','workspace','workspace.subscription','billing.subscription.write'),
  ('dashboard-overview','GET','/dashboard/overview','PRD-057','workspace','workspace.dashboard','dashboard.read'),
  ('dashboard-role-view','GET','/dashboard/role-view','PRD-057','workspace','workspace.dashboard','dashboard.read'),
  ('admin-assistant-query','POST','/admin-assistant/query','PRD-058','workspace','workspace.assistant','admin_assistant.query'),
  ('approvals-list','GET','/approvals','PRD-078','workspace','workspace.approval','approval.read'),
  ('approval-detail','GET','/approvals/{approval_id}','PRD-078','workspace','workspace.approval','approval.read'),
  ('approval-step-up','POST','/approvals/{approval_id}/step-up','PRD-078','workspace','workspace.approval','approval.decide'),
  ('approval-approve','POST','/approvals/{approval_id}/approve','PRD-078','workspace','workspace.approval','approval.decide'),
  ('approval-reject','POST','/approvals/{approval_id}/reject','PRD-078','workspace','workspace.approval','approval.decide'),
  ('user-preference-read','GET','/user/preferences','PRD-011','global_account','identity.account_preferences',NULL),
  ('user-preference-write','PATCH','/user/preferences','PRD-011','global_account','identity.account_preferences',NULL),
  ('internal-config-summary','GET','/internal/config/summary','PRD-010','platform','platform.config','internal.config.read'),
  ('console-navigation','GET','/console/navigation','PRD-059','workspace','workspace.navigation','console.access'),
  ('console-preference-read','GET','/console/preferences','PRD-059','workspace','identity.account_preferences',NULL),
  ('console-preference-write','PATCH','/console/preferences','PRD-059','workspace','identity.account_preferences',NULL),
  ('console-page-event','POST','/console/events','PRD-059','workspace','console_experience.events',NULL)
)
INSERT INTO legacy_route_catalog(
  legacy_route_id,route_code,method,normalized_path,source_prd,owner_domain,
  replacement_code,permission_code,retire_contract_epoch,
  production_manifest_digest,status,created_at)
SELECT 'lrt_'||substr(metahub_sha256('prd147/legacy-route/'||method||':'||normalized_path),1,32),
  route_code,method,normalized_path,source_prd,owner_domain,replacement_code,
  permission_code,1,
  metahub_sha256(method||'|'||normalized_path||'|'||owner_domain||'|'||coalesce(replacement_code,'none')||'|v1'),
  'planned',strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM route;

INSERT INTO console_ingress_contract_heads(
  head_key,route_deny_epoch,contract_generation,route_manifest_digest,
  legacy_write_fenced,status,revision,updated_at)
SELECT 'production',1,0,
  metahub_sha256(group_concat(method||':'||normalized_path||':'||production_manifest_digest,'|')),
  0,'expanded',1,strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM (SELECT method,normalized_path,production_manifest_digest
      FROM legacy_route_catalog ORDER BY method,normalized_path);

CREATE TRIGGER trg_legacy_route_catalog_insert_prd147
BEFORE INSERT ON legacy_route_catalog
BEGIN SELECT RAISE(ABORT,'legacy_route_catalog_immutable'); END;
CREATE TRIGGER trg_legacy_route_catalog_update_prd147
BEFORE UPDATE ON legacy_route_catalog
BEGIN SELECT RAISE(ABORT,'legacy_route_catalog_immutable'); END;
CREATE TRIGGER trg_legacy_route_catalog_delete_prd147
BEFORE DELETE ON legacy_route_catalog
BEGIN SELECT RAISE(ABORT,'legacy_route_catalog_immutable'); END;

CREATE TRIGGER trg_scheduler_registry_insert_prd147
BEFORE INSERT ON scheduler_registry_entries
BEGIN SELECT RAISE(ABORT,'scheduler_registry_manifest_immutable'); END;
CREATE TRIGGER trg_scheduler_registry_update_prd147
BEFORE UPDATE ON scheduler_registry_entries
BEGIN SELECT RAISE(ABORT,'scheduler_registry_manifest_immutable'); END;
CREATE TRIGGER trg_scheduler_registry_delete_prd147
BEFORE DELETE ON scheduler_registry_entries
BEGIN SELECT RAISE(ABORT,'scheduler_registry_manifest_immutable'); END;
CREATE TRIGGER trg_scheduler_mapping_insert_prd147
BEFORE INSERT ON scheduler_baseline_mappings
BEGIN SELECT RAISE(ABORT,'scheduler_baseline_mapping_immutable'); END;
CREATE TRIGGER trg_scheduler_mapping_update_prd147
BEFORE UPDATE ON scheduler_baseline_mappings
BEGIN SELECT RAISE(ABORT,'scheduler_baseline_mapping_immutable'); END;
CREATE TRIGGER trg_scheduler_mapping_delete_prd147
BEFORE DELETE ON scheduler_baseline_mappings
BEGIN SELECT RAISE(ABORT,'scheduler_baseline_mapping_immutable'); END;
CREATE TRIGGER trg_scheduler_queue_insert_prd147
BEFORE INSERT ON scheduler_queue_contracts
BEGIN SELECT RAISE(ABORT,'scheduler_queue_contract_immutable'); END;
CREATE TRIGGER trg_scheduler_queue_update_prd147
BEFORE UPDATE ON scheduler_queue_contracts
BEGIN SELECT RAISE(ABORT,'scheduler_queue_contract_immutable'); END;
CREATE TRIGGER trg_scheduler_queue_delete_prd147
BEFORE DELETE ON scheduler_queue_contracts
BEGIN SELECT RAISE(ABORT,'scheduler_queue_contract_immutable'); END;
CREATE TRIGGER trg_runtime_surface_insert_prd147
BEFORE INSERT ON runtime_surface_manifest
BEGIN SELECT RAISE(ABORT,'runtime_surface_manifest_immutable'); END;
CREATE TRIGGER trg_runtime_surface_update_prd147
BEFORE UPDATE ON runtime_surface_manifest
BEGIN SELECT RAISE(ABORT,'runtime_surface_manifest_immutable'); END;
CREATE TRIGGER trg_runtime_surface_delete_prd147
BEFORE DELETE ON runtime_surface_manifest
BEGIN SELECT RAISE(ABORT,'runtime_surface_manifest_immutable'); END;

CREATE TRIGGER trg_console_migration_state_prd147
BEFORE UPDATE OF state,operation_generation,revision ON console_domain_migrations
FOR EACH ROW WHEN
  NEW.operation_generation!=OLD.operation_generation
  OR NEW.revision!=OLD.revision+1
  OR NOT (
    NEW.state=OLD.state
    OR (OLD.state='inventoried' AND NEW.state IN('backfill_queued','blocked'))
    OR (OLD.state='backfill_queued' AND NEW.state IN('backfilling','blocked'))
    OR (OLD.state='backfilling' AND NEW.state IN('shadowing','blocked'))
    OR (OLD.state='shadowing' AND NEW.state IN('ready','blocked'))
    OR (OLD.state='ready' AND NEW.state IN('quiescing','rollback_queued','blocked'))
    OR (OLD.state='quiescing' AND NEW.state IN('cutting_over','rollback_queued','blocked'))
    OR (OLD.state='cutting_over' AND NEW.state IN('validating','rollback_queued','blocked'))
    OR (OLD.state='validating' AND NEW.state IN('active','rollback_queued','blocked'))
    OR (OLD.state='active' AND NEW.state='rollback_queued')
    OR (OLD.state='rollback_queued' AND NEW.state IN('rolling_back','blocked'))
    OR (OLD.state='rolling_back' AND NEW.state IN('rolled_back','blocked'))
    OR (OLD.state='blocked' AND NEW.state IN(
      'inventoried','backfill_queued','backfilling','shadowing','ready','quiescing',
      'cutting_over','validating','rollback_queued','rolling_back'))
  )
BEGIN SELECT RAISE(ABORT,'console_domain_migration_transition_invalid'); END;

CREATE TRIGGER trg_workspace_route_tombstone_state_prd147
BEFORE UPDATE OF status,revision ON workspace_legacy_route_tombstones
FOR EACH ROW WHEN NEW.revision!=OLD.revision+1 OR NOT(
  NEW.status=OLD.status
  OR (OLD.status='planned' AND NEW.status='write_fenced')
  OR (OLD.status='write_fenced' AND NEW.status='router_removed')
  OR (OLD.status='router_removed' AND NEW.status='verified'))
BEGIN SELECT RAISE(ABORT,'legacy_route_tombstone_transition_invalid'); END;
CREATE TRIGGER trg_platform_route_retirement_state_prd147
BEFORE UPDATE OF status,revision ON platform_legacy_route_retirements
FOR EACH ROW WHEN NEW.revision!=OLD.revision+1 OR NOT(
  NEW.status=OLD.status
  OR (OLD.status='planned' AND NEW.status='write_fenced')
  OR (OLD.status='write_fenced' AND NEW.status='router_removed')
  OR (OLD.status='router_removed' AND NEW.status='verified'))
BEGIN SELECT RAISE(ABORT,'legacy_route_tombstone_transition_invalid'); END;

CREATE TRIGGER trg_scheduler_watermark_fence_prd147
BEFORE INSERT ON scheduler_watermarks FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM scheduler_leases lease
  WHERE lease.scheduler_code=NEW.scheduler_code
    AND lease.partition_code=NEW.partition_code
    AND lease.fencing_generation=NEW.last_committed_fence
    AND unixepoch(lease.lease_deadline_at)>unixepoch('now'))
BEGIN SELECT RAISE(ABORT,'scheduler_fence_invalid'); END;
CREATE TRIGGER trg_scheduler_watermark_advance_prd147
BEFORE UPDATE ON scheduler_watermarks FOR EACH ROW
WHEN NEW.scheduler_code!=OLD.scheduler_code
  OR NEW.partition_code!=OLD.partition_code
  OR NEW.source_code!=OLD.source_code
  OR NEW.business_generation<OLD.business_generation
  OR NEW.last_committed_fence<OLD.last_committed_fence
  OR NEW.revision!=OLD.revision+1
  OR NOT EXISTS(
    SELECT 1 FROM scheduler_leases lease
    WHERE lease.scheduler_code=NEW.scheduler_code
      AND lease.partition_code=NEW.partition_code
      AND lease.fencing_generation=NEW.last_committed_fence
      AND unixepoch(lease.lease_deadline_at)>unixepoch('now'))
BEGIN SELECT RAISE(ABORT,'scheduler_fence_invalid'); END;

CREATE TRIGGER trg_console_migration_event_update_prd147
BEFORE UPDATE ON platform_console_migration_events
BEGIN SELECT RAISE(ABORT,'console_migration_event_immutable'); END;
CREATE TRIGGER trg_console_migration_event_delete_prd147
BEFORE DELETE ON platform_console_migration_events
BEGIN SELECT RAISE(ABORT,'console_migration_event_immutable'); END;
CREATE TRIGGER trg_console_migration_audit_update_prd147
BEFORE UPDATE ON platform_console_migration_audits
BEGIN SELECT RAISE(ABORT,'console_migration_audit_immutable'); END;
CREATE TRIGGER trg_console_migration_audit_delete_prd147
BEFORE DELETE ON platform_console_migration_audits
BEGIN SELECT RAISE(ABORT,'console_migration_audit_immutable'); END;
CREATE TRIGGER trg_console_backfill_checkpoint_update_prd147
BEFORE UPDATE ON console_backfill_checkpoints
BEGIN SELECT RAISE(ABORT,'console_backfill_checkpoint_immutable'); END;
CREATE TRIGGER trg_console_backfill_checkpoint_delete_prd147
BEFORE DELETE ON console_backfill_checkpoints
BEGIN SELECT RAISE(ABORT,'console_backfill_checkpoint_immutable'); END;

INSERT INTO platform_job_types(
  type_code,contract_version,artifact_sha256,queue_code,resource_class,
  max_attempts,timeout_seconds,status,revision,created_at,updated_at)
VALUES(
  'console.domain.migrate',1,
  '3dc5425d407ced943e122242ff502637ef7aad664478b1f79cb297e4a5622e9a',
  'bulk','io',10,18000,'active',1,
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'));

CREATE TEMP TABLE prd147_post_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd147_post_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok')
  + CASE WHEN (SELECT COUNT(*) FROM platform_console_migration_permission_extensions)=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_console_migration_role_permissions)=7 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM legacy_route_catalog)=46 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM scheduler_registry_entries)=37 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM scheduler_baseline_mappings)=41 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM scheduler_queue_contracts)=64 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM runtime_surface_manifest)=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM scheduler_registry_heads)=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM console_ingress_contract_heads)=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_job_types
      WHERE type_code='console.domain.migrate' AND contract_version=1
        AND artifact_sha256='3dc5425d407ced943e122242ff502637ef7aad664478b1f79cb297e4a5622e9a'
        AND queue_code='bulk' AND resource_class='io' AND max_attempts=10
        AND timeout_seconds=18000 AND status='active' AND revision=1)=1
      THEN 0 ELSE 1 END;
DROP TABLE prd147_post_guard;

COMMIT;
PRAGMA foreign_keys = ON;
