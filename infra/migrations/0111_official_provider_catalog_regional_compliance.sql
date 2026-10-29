-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd119_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd119_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0110' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'providers','provider_credentials','workspace_tenant_bindings','platform_roles',
      'platform_operators','workspace_machine_permission_extensions',
      'tenant_deletion_table_manifest'))=7 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd119')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd119_up_guard;

CREATE TABLE workspace_provider_catalog_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code='provider_catalog.read'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-119'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=30)
);
CREATE TABLE workspace_provider_catalog_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_provider_catalog_permission_extensions(permission_code)
);
INSERT INTO workspace_provider_catalog_permission_extensions VALUES
  ('provider_catalog.read','PRD-119',30);
INSERT INTO workspace_provider_catalog_role_permissions VALUES
  ('owner','provider_catalog.read'),
  ('admin','provider_catalog.read'),
  ('developer','provider_catalog.read');
CREATE TRIGGER trg_workspace_provider_catalog_permissions_insert_prd119
BEFORE INSERT ON workspace_provider_catalog_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_provider_catalog_permission_immutable'); END;
CREATE TRIGGER trg_workspace_provider_catalog_permissions_update_prd119
BEFORE UPDATE ON workspace_provider_catalog_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_provider_catalog_permission_immutable'); END;
CREATE TRIGGER trg_workspace_provider_catalog_permissions_delete_prd119
BEFORE DELETE ON workspace_provider_catalog_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_provider_catalog_permission_immutable'); END;
CREATE TRIGGER trg_workspace_provider_catalog_roles_insert_prd119
BEFORE INSERT ON workspace_provider_catalog_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_provider_catalog_permission_immutable'); END;
CREATE TRIGGER trg_workspace_provider_catalog_roles_update_prd119
BEFORE UPDATE ON workspace_provider_catalog_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_provider_catalog_permission_immutable'); END;
CREATE TRIGGER trg_workspace_provider_catalog_roles_delete_prd119
BEFORE DELETE ON workspace_provider_catalog_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_provider_catalog_permission_immutable'); END;

CREATE TABLE platform_provider_catalog_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.provider_catalog.manage','platform.provider_catalog.security_read')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-119'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=17)
);
CREATE TABLE platform_provider_catalog_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_provider_catalog_permission_extensions(permission_code)
);
INSERT INTO platform_provider_catalog_permission_extensions VALUES
  ('platform.provider_catalog.manage','PRD-119',17),
  ('platform.provider_catalog.security_read','PRD-119',17);
INSERT INTO platform_provider_catalog_role_permissions VALUES
  ('platform_owner','platform.provider_catalog.manage'),
  ('platform_owner','platform.provider_catalog.security_read'),
  ('platform_ops','platform.provider_catalog.manage'),
  ('platform_ops','platform.provider_catalog.security_read'),
  ('release_manager','platform.provider_catalog.manage'),
  ('release_manager','platform.provider_catalog.security_read'),
  ('platform_security','platform.provider_catalog.security_read');
CREATE TRIGGER trg_platform_provider_catalog_permissions_insert_prd119
BEFORE INSERT ON platform_provider_catalog_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_provider_catalog_permission_immutable'); END;
CREATE TRIGGER trg_platform_provider_catalog_permissions_update_prd119
BEFORE UPDATE ON platform_provider_catalog_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_provider_catalog_permission_immutable'); END;
CREATE TRIGGER trg_platform_provider_catalog_permissions_delete_prd119
BEFORE DELETE ON platform_provider_catalog_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_provider_catalog_permission_immutable'); END;
CREATE TRIGGER trg_platform_provider_catalog_roles_insert_prd119
BEFORE INSERT ON platform_provider_catalog_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_provider_catalog_permission_immutable'); END;
CREATE TRIGGER trg_platform_provider_catalog_roles_update_prd119
BEFORE UPDATE ON platform_provider_catalog_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_provider_catalog_permission_immutable'); END;
CREATE TRIGGER trg_platform_provider_catalog_roles_delete_prd119
BEFORE DELETE ON platform_provider_catalog_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_provider_catalog_permission_immutable'); END;

-- Deployment automation may append active trust keys. Existing key material is immutable.
CREATE TABLE provider_catalog_trusted_keys(
  key_id TEXT PRIMARY KEY CHECK(length(key_id) BETWEEN 3 AND 128
    AND key_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
  algorithm TEXT NOT NULL CHECK(algorithm='ed25519'),
  public_key_b64 TEXT NOT NULL CHECK(length(public_key_b64)=43
    AND public_key_b64 NOT GLOB '*[^A-Za-z0-9_-]*'),
  status TEXT NOT NULL CHECK(status IN('active','seed_only','retired')),
  key_purpose TEXT NOT NULL CHECK(key_purpose='provider_catalog'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(algorithm,public_key_b64)
);
INSERT INTO provider_catalog_trusted_keys VALUES(
  'metahub-provider-catalog-seed-v1','ed25519',
  'yAzhDbHRcIrGlcFplSYsEvxX-WcshkWq_0Fd7P4lXXo','seed_only',
  'provider_catalog','2026-08-26T00:00:00.000Z');
CREATE TRIGGER trg_provider_catalog_trusted_keys_update_prd119
BEFORE UPDATE ON provider_catalog_trusted_keys
BEGIN SELECT RAISE(ABORT,'provider_catalog_trust_key_immutable'); END;
CREATE TRIGGER trg_provider_catalog_trusted_keys_delete_prd119
BEFORE DELETE ON provider_catalog_trusted_keys
BEGIN SELECT RAISE(ABORT,'provider_catalog_trust_key_immutable'); END;

CREATE TABLE provider_catalog_versions(
  version TEXT PRIMARY KEY CHECK(version GLOB
    '[0-9][0-9][0-9][0-9].[0-9][0-9].[0-9][0-9].[1-9]*'),
  generation INTEGER NOT NULL UNIQUE CHECK(generation>=1),
  schema_version INTEGER NOT NULL CHECK(schema_version=1),
  manifest_sha256 TEXT NOT NULL UNIQUE CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  key_id TEXT NOT NULL,
  signature_algorithm TEXT NOT NULL CHECK(signature_algorithm='ed25519'),
  signature_b64 TEXT NOT NULL CHECK(length(signature_b64)=86
    AND signature_b64 NOT GLOB '*[^A-Za-z0-9_-]*'),
  entry_count INTEGER NOT NULL CHECK(entry_count BETWEEN 10 AND 100),
  compatibility_change_count INTEGER NOT NULL CHECK(
    compatibility_change_count BETWEEN 0 AND entry_count),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'seed_catalog','catalog_release','compliance_update','endpoint_update')),
  published_by_operator_id TEXT,
  published_at TEXT NOT NULL CHECK(unixepoch(published_at) IS NOT NULL),
  FOREIGN KEY(key_id) REFERENCES provider_catalog_trusted_keys(key_id),
  FOREIGN KEY(published_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((generation=1 AND reason_code='seed_catalog'
      AND published_by_operator_id IS NULL AND compatibility_change_count=0)
    OR(generation>1 AND reason_code!='seed_catalog'
      AND published_by_operator_id IS NOT NULL))
);
CREATE TRIGGER trg_provider_catalog_versions_update_prd119
BEFORE UPDATE ON provider_catalog_versions
BEGIN SELECT RAISE(ABORT,'provider_catalog_version_immutable'); END;
CREATE TRIGGER trg_provider_catalog_versions_delete_prd119
BEFORE DELETE ON provider_catalog_versions
BEGIN SELECT RAISE(ABORT,'provider_catalog_version_immutable'); END;

CREATE TABLE provider_catalog_head(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  version TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL UNIQUE CHECK(generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(version) REFERENCES provider_catalog_versions(version),
  FOREIGN KEY(generation) REFERENCES provider_catalog_versions(generation)
);
CREATE TRIGGER trg_provider_catalog_head_update_prd119
BEFORE UPDATE ON provider_catalog_head FOR EACH ROW
WHEN NEW.singleton!=OLD.singleton OR NEW.generation!=OLD.generation+1
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR NOT EXISTS(SELECT 1 FROM provider_catalog_versions version
      WHERE version.version=NEW.version AND version.generation=NEW.generation)
BEGIN SELECT RAISE(ABORT,'provider_catalog_head_transition_invalid'); END;
CREATE TRIGGER trg_provider_catalog_head_delete_prd119
BEFORE DELETE ON provider_catalog_head
BEGIN SELECT RAISE(ABORT,'provider_catalog_head_immutable'); END;

CREATE TABLE provider_catalog_entries(
  version TEXT NOT NULL,
  entry_code TEXT NOT NULL CHECK(length(entry_code) BETWEEN 2 AND 64
    AND entry_code=lower(entry_code) AND entry_code NOT GLOB '*[^a-z0-9_]*'),
  vendor_code TEXT NOT NULL CHECK(length(vendor_code) BETWEEN 2 AND 64
    AND vendor_code=lower(vendor_code) AND vendor_code NOT GLOB '*[^a-z0-9_]*'),
  display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 2 AND 120),
  provider_type TEXT NOT NULL CHECK(provider_type IN(
    'openai_compatible','zhipu','dashscope','wenxin','volcengine','hunyuan',
    'deepseek','custom','migration_compatible','local_endpoint')),
  service_class TEXT NOT NULL CHECK(service_class IN(
    'official_cloud','local_template','migration_source')),
  auth_modes_json TEXT NOT NULL CHECK(json_valid(auth_modes_json)
    AND json_type(auth_modes_json)='array' AND length(auth_modes_json)<=128),
  data_regions_json TEXT NOT NULL CHECK(json_valid(data_regions_json)
    AND json_type(data_regions_json)='array' AND length(data_regions_json)<=1024),
  compliance_tags_json TEXT NOT NULL CHECK(json_valid(compliance_tags_json)
    AND json_type(compliance_tags_json)='array' AND length(compliance_tags_json)<=1024),
  status TEXT NOT NULL CHECK(status IN('active','deprecated','retired')),
  instantiation_mode TEXT NOT NULL CHECK(instantiation_mode IN(
    'provider','local_endpoint','migration_only')),
  PRIMARY KEY(version,entry_code),
  FOREIGN KEY(version) REFERENCES provider_catalog_versions(version),
  CHECK((instantiation_mode='provider' AND service_class='official_cloud' AND status='active')
    OR(instantiation_mode='local_endpoint' AND service_class='local_template')
    OR(instantiation_mode='migration_only' AND service_class='migration_source'
      AND status IN('deprecated','retired')))
);
CREATE INDEX idx_provider_catalog_entries_filter_prd119
  ON provider_catalog_entries(version,status,vendor_code,entry_code);
CREATE TRIGGER trg_provider_catalog_entries_update_prd119
BEFORE UPDATE ON provider_catalog_entries
BEGIN SELECT RAISE(ABORT,'provider_catalog_entry_immutable'); END;
CREATE TRIGGER trg_provider_catalog_entries_delete_prd119
BEFORE DELETE ON provider_catalog_entries
BEGIN SELECT RAISE(ABORT,'provider_catalog_entry_immutable'); END;

CREATE TABLE provider_capabilities(
  version TEXT NOT NULL,
  entry_code TEXT NOT NULL,
  capability_code TEXT NOT NULL CHECK(length(capability_code) BETWEEN 2 AND 64
    AND capability_code=lower(capability_code)
    AND capability_code NOT GLOB '*[^a-z0-9_]*'),
  max_context_tokens INTEGER NOT NULL CHECK(max_context_tokens BETWEEN 1 AND 10000000),
  supports_stream INTEGER NOT NULL CHECK(supports_stream IN(0,1)),
  supports_tools INTEGER NOT NULL CHECK(supports_tools IN(0,1)),
  supports_files INTEGER NOT NULL CHECK(supports_files IN(0,1)),
  PRIMARY KEY(version,entry_code,capability_code),
  FOREIGN KEY(version,entry_code)
    REFERENCES provider_catalog_entries(version,entry_code)
);
CREATE INDEX idx_provider_capability_filter_prd119
  ON provider_capabilities(version,capability_code,entry_code);
CREATE TRIGGER trg_provider_capabilities_update_prd119
BEFORE UPDATE ON provider_capabilities
BEGIN SELECT RAISE(ABORT,'provider_capability_immutable'); END;
CREATE TRIGGER trg_provider_capabilities_delete_prd119
BEFORE DELETE ON provider_capabilities
BEGIN SELECT RAISE(ABORT,'provider_capability_immutable'); END;

CREATE TABLE regional_endpoint_templates(
  version TEXT NOT NULL,
  entry_code TEXT NOT NULL,
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 2 AND 40
    AND region_code=lower(region_code) AND region_code NOT GLOB '*[^a-z0-9-]*'),
  cloud_service_code TEXT NOT NULL CHECK(length(cloud_service_code) BETWEEN 2 AND 64
    AND cloud_service_code=lower(cloud_service_code)
    AND cloud_service_code NOT GLOB '*[^a-z0-9_]*'),
  scheme TEXT NOT NULL CHECK(scheme='https'),
  host TEXT NOT NULL CHECK(length(host) BETWEEN 4 AND 253
    AND host=lower(host) AND instr(host,'*')=0 AND instr(host,'@')=0
    AND instr(host,'/')=0 AND instr(host,'?')=0 AND instr(host,'#')=0),
  path_template TEXT NOT NULL CHECK(length(path_template) BETWEEN 1 AND 256
    AND substr(path_template,1,1)='/' AND instr(path_template,'?')=0
    AND instr(path_template,'#')=0),
  residency_tags_json TEXT NOT NULL CHECK(json_valid(residency_tags_json)
    AND json_type(residency_tags_json)='array' AND length(residency_tags_json)<=1024),
  PRIMARY KEY(version,entry_code,region_code),
  FOREIGN KEY(version,entry_code)
    REFERENCES provider_catalog_entries(version,entry_code)
);
CREATE INDEX idx_regional_endpoint_region_prd119
  ON regional_endpoint_templates(version,region_code,entry_code);
CREATE TRIGGER trg_regional_endpoint_templates_update_prd119
BEFORE UPDATE ON regional_endpoint_templates
BEGIN SELECT RAISE(ABORT,'regional_endpoint_template_immutable'); END;
CREATE TRIGGER trg_regional_endpoint_templates_delete_prd119
BEFORE DELETE ON regional_endpoint_templates
BEGIN SELECT RAISE(ABORT,'regional_endpoint_template_immutable'); END;

CREATE TABLE compatibility_profiles(
  version TEXT NOT NULL,
  entry_code TEXT NOT NULL,
  compatibility_level TEXT NOT NULL CHECK(compatibility_level IN(
    'native','openai_compatible','migration_only')),
  doc_code TEXT NOT NULL CHECK(length(doc_code) BETWEEN 3 AND 128
    AND doc_code=lower(doc_code) AND doc_code NOT GLOB '*[^a-z0-9_.]*'),
  migration_source_code TEXT CHECK(migration_source_code IS NULL OR(
    length(migration_source_code) BETWEEN 2 AND 64
    AND migration_source_code=lower(migration_source_code)
    AND migration_source_code NOT GLOB '*[^a-z0-9_]*')),
  request_difference_codes_json TEXT NOT NULL CHECK(
    json_valid(request_difference_codes_json)
    AND json_type(request_difference_codes_json)='array'
    AND length(request_difference_codes_json)<=2048),
  PRIMARY KEY(version,entry_code),
  FOREIGN KEY(version,entry_code)
    REFERENCES provider_catalog_entries(version,entry_code),
  CHECK((compatibility_level='migration_only' AND migration_source_code IS NOT NULL)
    OR(compatibility_level!='migration_only' AND migration_source_code IS NULL))
);
CREATE TRIGGER trg_compatibility_profiles_update_prd119
BEFORE UPDATE ON compatibility_profiles
BEGIN SELECT RAISE(ABORT,'compatibility_profile_immutable'); END;
CREATE TRIGGER trg_compatibility_profiles_delete_prd119
BEFORE DELETE ON compatibility_profiles
BEGIN SELECT RAISE(ABORT,'compatibility_profile_immutable'); END;

CREATE TABLE provider_catalog_instances(
  provider_catalog_instance_id TEXT PRIMARY KEY CHECK(
    length(provider_catalog_instance_id)=36
    AND substr(provider_catalog_instance_id,1,4)='pci_'
    AND substr(provider_catalog_instance_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  entry_code TEXT NOT NULL,
  catalog_version TEXT NOT NULL,
  region_code TEXT NOT NULL,
  capability_codes_json TEXT NOT NULL CHECK(json_valid(capability_codes_json)
    AND json_type(capability_codes_json)='array' AND length(capability_codes_json)<=2048),
  endpoint_sha256 TEXT NOT NULL CHECK(length(endpoint_sha256)=64
    AND endpoint_sha256 NOT GLOB '*[^0-9a-f]*'),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_account_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,provider_id),
  FOREIGN KEY(catalog_version,entry_code)
    REFERENCES provider_catalog_entries(version,entry_code),
  FOREIGN KEY(created_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,provider_id),
  UNIQUE(workspace_id,tenant_id,provider_catalog_instance_id)
);
CREATE INDEX idx_provider_catalog_instances_owner_prd119
  ON provider_catalog_instances(workspace_id,tenant_id,entry_code,catalog_version);
CREATE TRIGGER trg_provider_catalog_instances_update_prd119
BEFORE UPDATE ON provider_catalog_instances
BEGIN SELECT RAISE(ABORT,'provider_catalog_instance_immutable'); END;
CREATE TRIGGER trg_provider_catalog_instances_delete_prd119
BEFORE DELETE ON provider_catalog_instances
BEGIN SELECT RAISE(ABORT,'provider_catalog_instance_immutable'); END;

CREATE TABLE provider_preflight_runs(
  provider_preflight_run_id TEXT PRIMARY KEY CHECK(
    length(provider_preflight_run_id)=36
    AND substr(provider_preflight_run_id,1,4)='pfr_'
    AND substr(provider_preflight_run_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  provider_catalog_instance_id TEXT NOT NULL,
  instance_revision INTEGER NOT NULL CHECK(instance_revision>=1),
  status TEXT NOT NULL CHECK(status IN(
    'succeeded','failed','inconclusive','timed_out')),
  outcome_code TEXT NOT NULL CHECK(length(outcome_code) BETWEEN 2 AND 64
    AND outcome_code=lower(outcome_code) AND outcome_code NOT GLOB '*[^a-z0-9_]*'),
  latency_ms INTEGER NOT NULL CHECK(latency_ms BETWEEN 0 AND 30000),
  http_status INTEGER CHECK(http_status IS NULL OR http_status BETWEEN 100 AND 599),
  capability_sha256 TEXT NOT NULL CHECK(length(capability_sha256)=64
    AND capability_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  checked_at TEXT NOT NULL CHECK(unixepoch(checked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,provider_catalog_instance_id)
    REFERENCES provider_catalog_instances(
      workspace_id,tenant_id,provider_catalog_instance_id),
  FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,provider_id),
  UNIQUE(workspace_id,tenant_id,provider_preflight_run_id)
);
CREATE INDEX idx_provider_preflight_owner_prd119
  ON provider_preflight_runs(workspace_id,tenant_id,provider_id,checked_at);
CREATE TRIGGER trg_provider_preflight_runs_update_prd119
BEFORE UPDATE ON provider_preflight_runs
BEGIN SELECT RAISE(ABORT,'provider_preflight_run_immutable'); END;
CREATE TRIGGER trg_provider_preflight_runs_delete_prd119
BEFORE DELETE ON provider_preflight_runs
BEGIN SELECT RAISE(ABORT,'provider_preflight_run_immutable'); END;

CREATE TABLE workspace_provider_catalog_commands(
  provider_catalog_command_id TEXT PRIMARY KEY CHECK(
    length(provider_catalog_command_id)=36
    AND substr(provider_catalog_command_id,1,4)='pcc_'
    AND substr(provider_catalog_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_account_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'provider.instantiate','provider.preflight')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id) BETWEEN 4 AND 160),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,actor_account_id,action_code,idempotency_sha256),
  UNIQUE(workspace_id,tenant_id,provider_catalog_command_id)
);
CREATE TRIGGER trg_workspace_provider_catalog_commands_update_prd119
BEFORE UPDATE ON workspace_provider_catalog_commands
BEGIN SELECT RAISE(ABORT,'provider_catalog_command_immutable'); END;
CREATE TRIGGER trg_workspace_provider_catalog_commands_delete_prd119
BEFORE DELETE ON workspace_provider_catalog_commands
BEGIN SELECT RAISE(ABORT,'provider_catalog_command_immutable'); END;

CREATE TABLE platform_provider_catalog_commands(
  platform_provider_catalog_command_id TEXT PRIMARY KEY CHECK(
    length(platform_provider_catalog_command_id)=36
    AND substr(platform_provider_catalog_command_id,1,4)='ppc_'
    AND substr(platform_provider_catalog_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  operator_id TEXT NOT NULL,
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_version TEXT NOT NULL,
  result_generation INTEGER NOT NULL CHECK(result_generation>=2),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(result_version) REFERENCES provider_catalog_versions(version),
  FOREIGN KEY(result_generation) REFERENCES provider_catalog_versions(generation),
  UNIQUE(operator_id,idempotency_sha256)
);
CREATE TRIGGER trg_platform_provider_catalog_commands_update_prd119
BEFORE UPDATE ON platform_provider_catalog_commands
BEGIN SELECT RAISE(ABORT,'platform_provider_catalog_command_immutable'); END;
CREATE TRIGGER trg_platform_provider_catalog_commands_delete_prd119
BEFORE DELETE ON platform_provider_catalog_commands
BEGIN SELECT RAISE(ABORT,'platform_provider_catalog_command_immutable'); END;

CREATE TABLE provider_catalog_events(
  provider_catalog_event_id TEXT PRIMARY KEY CHECK(
    length(provider_catalog_event_id)=36
    AND substr(provider_catalog_event_id,1,4)='pce_'
    AND substr(provider_catalog_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'provider_catalog.updated','provider.preflight_completed',
    'provider.compatibility_changed')),
  workspace_id TEXT,
  tenant_id TEXT,
  provider_id TEXT,
  provider_preflight_run_id TEXT,
  catalog_version TEXT NOT NULL,
  entry_code TEXT,
  outcome_code TEXT NOT NULL CHECK(length(outcome_code) BETWEEN 2 AND 64
    AND outcome_code=lower(outcome_code) AND outcome_code NOT GLOB '*[^a-z0-9_]*'),
  revision INTEGER NOT NULL CHECK(revision>=1),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,provider_id),
  FOREIGN KEY(provider_preflight_run_id)
    REFERENCES provider_preflight_runs(provider_preflight_run_id),
  FOREIGN KEY(catalog_version) REFERENCES provider_catalog_versions(version),
  FOREIGN KEY(catalog_version,entry_code)
    REFERENCES provider_catalog_entries(version,entry_code),
  CHECK((event_type='provider_catalog.updated' AND workspace_id IS NULL
      AND tenant_id IS NULL AND provider_id IS NULL
      AND provider_preflight_run_id IS NULL AND entry_code IS NULL)
    OR(event_type='provider.compatibility_changed' AND workspace_id IS NULL
      AND tenant_id IS NULL AND provider_id IS NULL
      AND provider_preflight_run_id IS NULL AND entry_code IS NOT NULL)
    OR(event_type='provider.preflight_completed' AND workspace_id IS NOT NULL
      AND tenant_id IS NOT NULL AND provider_id IS NOT NULL
      AND provider_preflight_run_id IS NOT NULL AND entry_code IS NOT NULL))
);
CREATE INDEX idx_provider_catalog_events_owner_prd119
  ON provider_catalog_events(tenant_id,workspace_id,event_type,occurred_at);
CREATE TRIGGER trg_provider_catalog_events_update_prd119
BEFORE UPDATE ON provider_catalog_events
BEGIN SELECT RAISE(ABORT,'provider_catalog_event_immutable'); END;
CREATE TRIGGER trg_provider_catalog_events_delete_prd119
BEFORE DELETE ON provider_catalog_events
BEGIN SELECT RAISE(ABORT,'provider_catalog_event_immutable'); END;

CREATE TABLE provider_catalog_event_outbox(
  provider_catalog_outbox_id TEXT PRIMARY KEY CHECK(
    length(provider_catalog_outbox_id)=36
    AND substr(provider_catalog_outbox_id,1,4)='pco_'
    AND substr(provider_catalog_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  provider_catalog_event_id TEXT NOT NULL UNIQUE,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('platform','workspace')),
  status TEXT NOT NULL CHECK(status IN('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN(
    'delivery_unavailable','delivery_rejected')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(provider_catalog_event_id)
    REFERENCES provider_catalog_events(provider_catalog_event_id),
  CHECK((status='pending' AND delivered_at IS NULL AND last_error_code IS NULL)
    OR(status='delivered' AND delivered_at IS NOT NULL AND last_error_code IS NULL)
    OR(status='failed' AND delivered_at IS NULL AND last_error_code IS NOT NULL))
);
CREATE INDEX idx_provider_catalog_outbox_delivery_prd119
  ON provider_catalog_event_outbox(status,available_at,provider_catalog_outbox_id);
CREATE TRIGGER trg_provider_catalog_outbox_update_prd119
BEFORE UPDATE ON provider_catalog_event_outbox FOR EACH ROW
WHEN NEW.provider_catalog_outbox_id!=OLD.provider_catalog_outbox_id
  OR NEW.provider_catalog_event_id!=OLD.provider_catalog_event_id
  OR NEW.delivery_domain!=OLD.delivery_domain OR NEW.available_at!=OLD.available_at
  OR NEW.created_at!=OLD.created_at OR OLD.status='delivered'
  OR NEW.attempt_count!=OLD.attempt_count+1 OR NEW.updated_at<OLD.updated_at
  OR NOT((OLD.status='pending' AND NEW.status IN('delivered','failed'))
    OR(OLD.status='failed' AND NEW.status IN('delivered','failed')))
BEGIN SELECT RAISE(ABORT,'provider_catalog_outbox_transition_invalid'); END;
CREATE TRIGGER trg_provider_catalog_outbox_delete_prd119
BEFORE DELETE ON provider_catalog_event_outbox
BEGIN SELECT RAISE(ABORT,'provider_catalog_outbox_immutable'); END;

INSERT INTO provider_catalog_versions VALUES(
  '2026.08.26.1',1,1,
  'ca6612446264599d97322aed537feab22810093ceb6535db0eaf09d9ddf266d4',
  'metahub-provider-catalog-seed-v1','ed25519',
  '4-XWU0bwMKv9t4K2tvUPMhQXON2L54UVd4e1JI1wZgBlTKHoqvFTtM3zfhkIBrRupMm038uhOB0Wi_TR-8Z4Dg',
  10,0,'seed_catalog',NULL,'2026-08-26T00:00:00.000Z');

INSERT INTO provider_catalog_entries VALUES
  ('2026.08.26.1','dashscope','alibaba_cloud','Alibaba Cloud Model Studio','dashscope',
    'official_cloud','["bearer"]','["cn-beijing"]',
    '["cn_mainland","customer_credential"]','active','provider'),
  ('2026.08.26.1','deepseek','deepseek','DeepSeek','deepseek',
    'official_cloud','["bearer"]','["cn-mainland"]',
    '["cn_mainland","customer_credential"]','active','provider'),
  ('2026.08.26.1','hunyuan','tencent_cloud','Tencent Hunyuan','hunyuan',
    'official_cloud','["bearer"]','["ap-guangzhou"]',
    '["cn_mainland","customer_credential"]','active','provider'),
  ('2026.08.26.1','local_openai_compatible','customer_local',
    'Local OpenAI-compatible endpoint','local_endpoint','local_template',
    '["bearer","none"]','["customer-network"]',
    '["customer_managed","private_network"]','active','local_endpoint'),
  ('2026.08.26.1','moonshot','moonshot_ai','Moonshot AI','openai_compatible',
    'official_cloud','["bearer"]','["cn-mainland"]',
    '["cn_mainland","customer_credential"]','active','provider'),
  ('2026.08.26.1','openrouter_migration','openrouter','OpenRouter migration source',
    'migration_compatible','migration_source','["bearer"]','["global"]',
    '["migration_source_only"]','deprecated','migration_only'),
  ('2026.08.26.1','siliconflow_migration','siliconflow',
    'SiliconFlow migration source','migration_compatible','migration_source',
    '["bearer"]','["cn-mainland"]','["migration_source_only"]',
    'deprecated','migration_only'),
  ('2026.08.26.1','volcengine','volcengine','Volcengine Ark','volcengine',
    'official_cloud','["bearer"]','["cn-beijing"]',
    '["cn_mainland","customer_credential"]','active','provider'),
  ('2026.08.26.1','wenxin','baidu_cloud','Baidu Qianfan','wenxin',
    'official_cloud','["bearer"]','["cn-beijing"]',
    '["cn_mainland","customer_credential"]','active','provider'),
  ('2026.08.26.1','zhipu','zhipu_ai','Zhipu AI','zhipu',
    'official_cloud','["bearer"]','["cn-beijing"]',
    '["cn_mainland","customer_credential"]','active','provider');

INSERT INTO provider_capabilities VALUES
  ('2026.08.26.1','dashscope','chat',131072,1,1,0),
  ('2026.08.26.1','dashscope','embeddings',8192,0,0,0),
  ('2026.08.26.1','deepseek','chat',65536,1,1,0),
  ('2026.08.26.1','hunyuan','chat',32768,1,1,0),
  ('2026.08.26.1','local_openai_compatible','chat',32768,1,1,0),
  ('2026.08.26.1','local_openai_compatible','embeddings',8192,0,0,0),
  ('2026.08.26.1','moonshot','chat',131072,1,1,0),
  ('2026.08.26.1','openrouter_migration','chat',32768,1,1,0),
  ('2026.08.26.1','siliconflow_migration','chat',32768,1,1,0),
  ('2026.08.26.1','volcengine','chat',131072,1,1,0),
  ('2026.08.26.1','wenxin','chat',128000,1,1,0),
  ('2026.08.26.1','zhipu','chat',128000,1,1,0);

INSERT INTO regional_endpoint_templates VALUES
  ('2026.08.26.1','dashscope','cn-beijing','alibaba_model_studio','https',
    'dashscope.aliyuncs.com','/compatible-mode/v1','["cn-beijing"]'),
  ('2026.08.26.1','deepseek','cn-mainland','deepseek_official','https',
    'api.deepseek.com','/v1','["cn-mainland"]'),
  ('2026.08.26.1','hunyuan','ap-guangzhou','tencent_hunyuan','https',
    'api.hunyuan.cloud.tencent.com','/v1','["ap-guangzhou"]'),
  ('2026.08.26.1','moonshot','cn-mainland','moonshot_official','https',
    'api.moonshot.cn','/v1','["cn-mainland"]'),
  ('2026.08.26.1','volcengine','cn-beijing','volcengine_ark','https',
    'ark.cn-beijing.volces.com','/api/v3','["cn-beijing"]'),
  ('2026.08.26.1','wenxin','cn-beijing','baidu_qianfan','https',
    'qianfan.baidubce.com','/v2','["cn-beijing"]'),
  ('2026.08.26.1','zhipu','cn-beijing','zhipu_official','https',
    'open.bigmodel.cn','/api/paas/v4','["cn-beijing"]');

INSERT INTO compatibility_profiles VALUES
  ('2026.08.26.1','dashscope','native','provider_catalog.dashscope.native',NULL,'[]'),
  ('2026.08.26.1','deepseek','native','provider_catalog.deepseek.native',NULL,'[]'),
  ('2026.08.26.1','hunyuan','native','provider_catalog.hunyuan.native',NULL,'[]'),
  ('2026.08.26.1','local_openai_compatible','openai_compatible',
    'provider_catalog.local_openai_compatible.openai_compatible',NULL,'[]'),
  ('2026.08.26.1','moonshot','native','provider_catalog.moonshot.native',NULL,'[]'),
  ('2026.08.26.1','openrouter_migration','migration_only',
    'provider_catalog.openrouter_migration.migration_only','openrouter','[]'),
  ('2026.08.26.1','siliconflow_migration','migration_only',
    'provider_catalog.siliconflow_migration.migration_only','siliconflow','[]'),
  ('2026.08.26.1','volcengine','native','provider_catalog.volcengine.native',NULL,'[]'),
  ('2026.08.26.1','wenxin','native','provider_catalog.wenxin.native',NULL,'[]'),
  ('2026.08.26.1','zhipu','native','provider_catalog.zhipu.native',NULL,'[]');

INSERT INTO provider_catalog_head VALUES(
  1,'2026.08.26.1',1,1,'2026-08-26T00:00:00.000Z');
CREATE TRIGGER trg_provider_catalog_head_insert_prd119
BEFORE INSERT ON provider_catalog_head
BEGIN SELECT RAISE(ABORT,'provider_catalog_head_immutable'); END;
INSERT INTO provider_catalog_events VALUES(
  'pce_00000000000000000000000000000001','provider_catalog.updated',
  NULL,NULL,NULL,NULL,'2026.08.26.1',NULL,'seeded',1,
  'ca6612446264599d97322aed537feab22810093ceb6535db0eaf09d9ddf266d4',
  'migration-0111','migration-0111','2026-08-26T00:00:00.000Z');
INSERT INTO provider_catalog_event_outbox VALUES(
  'pco_00000000000000000000000000000001',
  'pce_00000000000000000000000000000001','platform','delivered',1,
  '2026-08-26T00:00:00.000Z','2026-08-26T00:00:00.000Z',NULL,
  '2026-08-26T00:00:00.000Z','2026-08-26T00:00:00.000Z');

-- Register every tenant-owned table in the PRD-089 deletion manifest.
CREATE TABLE tenant_deletion_manifest_extensions_prd119(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-119'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd119_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd119_manifest_sources VALUES
  ('provider_catalog_instances','delete',NULL),
  ('provider_preflight_runs','delete','operational'),
  ('workspace_provider_catalog_commands','delete',NULL),
  ('provider_catalog_events','archive_then_delete','audit');
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
JOIN prd119_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd119(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-119',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd119_manifest_sources ORDER BY source_table;
DROP TABLE prd119_manifest_sources;

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
CREATE TRIGGER trg_tenant_deletion_manifest_prd119_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd119
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd119_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd119
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd119_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd119
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd119_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd119_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_provider_catalog_permission_extensions)=1
    AND (SELECT COUNT(*) FROM workspace_provider_catalog_role_permissions)=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_provider_catalog_permission_extensions)=2
    AND (SELECT COUNT(*) FROM platform_provider_catalog_role_permissions)=7 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM provider_catalog_versions)=1
    AND (SELECT COUNT(*) FROM provider_catalog_entries)=10
    AND (SELECT COUNT(*) FROM provider_capabilities)=12
    AND (SELECT COUNT(*) FROM regional_endpoint_templates)=7
    AND (SELECT COUNT(*) FROM compatibility_profiles)=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd119)=4
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd119_up_final_guard;
COMMIT;
