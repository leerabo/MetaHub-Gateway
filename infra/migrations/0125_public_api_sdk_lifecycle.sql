-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd133_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd133_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0124' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_roles','platform_operators','global_accounts'))=3 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd133')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd133_up_guard;

CREATE TABLE platform_developer_contract_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.developer_contract.read','platform.developer_contract.manage',
    'platform.developer_contract.approve')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-133'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=45)
);
CREATE TABLE platform_developer_contract_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN('platform_owner','release_manager')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_developer_contract_permission_extensions(permission_code)
);
INSERT INTO platform_developer_contract_permission_extensions VALUES
  ('platform.developer_contract.read','PRD-133',45),
  ('platform.developer_contract.manage','PRD-133',45),
  ('platform.developer_contract.approve','PRD-133',45);
INSERT INTO platform_developer_contract_role_permissions VALUES
  ('platform_owner','platform.developer_contract.read'),
  ('platform_owner','platform.developer_contract.manage'),
  ('platform_owner','platform.developer_contract.approve'),
  ('release_manager','platform.developer_contract.read'),
  ('release_manager','platform.developer_contract.manage'),
  ('release_manager','platform.developer_contract.approve');
CREATE TRIGGER trg_platform_developer_contract_permissions_insert_prd133
BEFORE INSERT ON platform_developer_contract_permission_extensions
BEGIN SELECT RAISE(ABORT,'developer_contract_permission_immutable'); END;
CREATE TRIGGER trg_platform_developer_contract_permissions_update_prd133
BEFORE UPDATE ON platform_developer_contract_permission_extensions
BEGIN SELECT RAISE(ABORT,'developer_contract_permission_immutable'); END;
CREATE TRIGGER trg_platform_developer_contract_permissions_delete_prd133
BEFORE DELETE ON platform_developer_contract_permission_extensions
BEGIN SELECT RAISE(ABORT,'developer_contract_permission_immutable'); END;
CREATE TRIGGER trg_platform_developer_contract_roles_insert_prd133
BEFORE INSERT ON platform_developer_contract_role_permissions
BEGIN SELECT RAISE(ABORT,'developer_contract_permission_immutable'); END;
CREATE TRIGGER trg_platform_developer_contract_roles_update_prd133
BEFORE UPDATE ON platform_developer_contract_role_permissions
BEGIN SELECT RAISE(ABORT,'developer_contract_permission_immutable'); END;
CREATE TRIGGER trg_platform_developer_contract_roles_delete_prd133
BEFORE DELETE ON platform_developer_contract_role_permissions
BEGIN SELECT RAISE(ABORT,'developer_contract_permission_immutable'); END;

CREATE TABLE developer_contract_signing_keys(
  key_id TEXT PRIMARY KEY CHECK(length(key_id) BETWEEN 8 AND 80
    AND key_id=lower(key_id) AND key_id NOT GLOB '*[^a-z0-9._-]*'),
  purpose TEXT NOT NULL CHECK(purpose='metahub.public-contract.release/v1'),
  algorithm TEXT NOT NULL CHECK(algorithm='ed25519'),
  public_key_b64 TEXT NOT NULL CHECK(length(public_key_b64)=43),
  public_key_sha256 TEXT NOT NULL UNIQUE CHECK(length(public_key_sha256)=64
    AND public_key_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('active','revoked')),
  not_before TEXT NOT NULL CHECK(unixepoch(not_before) IS NOT NULL),
  not_after TEXT NOT NULL CHECK(unixepoch(not_after) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  CHECK(not_after>not_before)
);
CREATE TRIGGER trg_developer_contract_keys_update_prd133
BEFORE UPDATE ON developer_contract_signing_keys FOR EACH ROW
WHEN NEW.key_id!=OLD.key_id OR NEW.purpose!=OLD.purpose OR NEW.algorithm!=OLD.algorithm
  OR NEW.public_key_b64!=OLD.public_key_b64 OR NEW.public_key_sha256!=OLD.public_key_sha256
  OR NEW.not_before!=OLD.not_before OR NEW.not_after!=OLD.not_after
  OR NOT(OLD.status='active' AND NEW.status='revoked')
BEGIN SELECT RAISE(ABORT,'developer_contract_signing_key_immutable'); END;
CREATE TRIGGER trg_developer_contract_keys_delete_prd133
BEFORE DELETE ON developer_contract_signing_keys
BEGIN SELECT RAISE(ABORT,'developer_contract_signing_key_immutable'); END;

CREATE TABLE api_versions(
  api_version_id TEXT PRIMARY KEY CHECK(length(api_version_id)=36
    AND substr(api_version_id,1,4)='apv_' AND substr(api_version_id,5) NOT GLOB '*[^0-9a-f]*'),
  version TEXT NOT NULL UNIQUE CHECK(version GLOB '[0-9]*.[0-9]*.[0-9]*' AND length(version)<=32),
  registry_revision INTEGER NOT NULL CHECK(registry_revision>=1),
  status TEXT NOT NULL CHECK(status IN(
    'pending_approval','published','deprecated','sunset')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_operator_id TEXT NOT NULL,
  approved_by_operator_id TEXT,
  published_at TEXT CHECK(published_at IS NULL OR unixepoch(published_at) IS NOT NULL),
  deprecated_at TEXT CHECK(deprecated_at IS NULL OR unixepoch(deprecated_at) IS NOT NULL),
  sunset_at TEXT CHECK(sunset_at IS NULL OR unixepoch(sunset_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(approved_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status='pending_approval' AND approved_by_operator_id IS NULL AND published_at IS NULL)
    OR(status!='pending_approval' AND approved_by_operator_id IS NOT NULL AND published_at IS NOT NULL)),
  CHECK(created_by_operator_id!=coalesce(approved_by_operator_id,'')),
  CHECK((status IN('deprecated','sunset') AND deprecated_at IS NOT NULL)
    OR(status NOT IN('deprecated','sunset') AND deprecated_at IS NULL)),
  CHECK((status='sunset' AND sunset_at IS NOT NULL) OR status!='sunset')
);
CREATE TRIGGER trg_api_versions_update_prd133
BEFORE UPDATE ON api_versions FOR EACH ROW
WHEN NEW.api_version_id!=OLD.api_version_id OR NEW.version!=OLD.version
  OR NEW.registry_revision!=OLD.registry_revision OR NEW.created_by_operator_id!=OLD.created_by_operator_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='pending_approval' AND NEW.status='published')
    OR(OLD.status='published' AND NEW.status='deprecated')
    OR(OLD.status='deprecated' AND NEW.status='published')
    OR(OLD.status='deprecated' AND NEW.status='sunset'))
BEGIN SELECT RAISE(ABORT,'api_version_transition_invalid'); END;
CREATE TRIGGER trg_api_versions_delete_prd133 BEFORE DELETE ON api_versions
BEGIN SELECT RAISE(ABORT,'api_version_immutable'); END;

CREATE TABLE api_contract_snapshots(
  api_contract_snapshot_id TEXT PRIMARY KEY CHECK(length(api_contract_snapshot_id)=36
    AND substr(api_contract_snapshot_id,1,4)='acs_'
    AND substr(api_contract_snapshot_id,5) NOT GLOB '*[^0-9a-f]*'),
  api_version_id TEXT NOT NULL UNIQUE,
  public_registry_json TEXT NOT NULL CHECK(json_valid(public_registry_json)
    AND length(public_registry_json) BETWEEN 128 AND 262144),
  canonical_sha256 TEXT NOT NULL CHECK(length(canonical_sha256)=64
    AND canonical_sha256 NOT GLOB '*[^0-9a-f]*'),
  compatibility_baseline_sha256 TEXT CHECK(compatibility_baseline_sha256 IS NULL
    OR(length(compatibility_baseline_sha256)=64
      AND compatibility_baseline_sha256 NOT GLOB '*[^0-9a-f]*')),
  compatibility_outcome TEXT NOT NULL CHECK(compatibility_outcome IN('compatible','breaking')),
  breaking_reasons_json TEXT NOT NULL CHECK(json_valid(breaking_reasons_json)),
  producer_revision INTEGER NOT NULL CHECK(producer_revision>=1),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  build_sha256 TEXT NOT NULL CHECK(length(build_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('pending_approval','published','rejected')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  signing_key_id TEXT,
  signature_b64 TEXT CHECK(signature_b64 IS NULL OR length(signature_b64)=86),
  signature_sha256 TEXT CHECK(signature_sha256 IS NULL OR length(signature_sha256)=64),
  approved_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  approved_at TEXT CHECK(approved_at IS NULL OR unixepoch(approved_at) IS NOT NULL),
  FOREIGN KEY(api_version_id) REFERENCES api_versions(api_version_id),
  FOREIGN KEY(signing_key_id) REFERENCES developer_contract_signing_keys(key_id),
  FOREIGN KEY(approved_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status='pending_approval' AND signing_key_id IS NULL AND signature_b64 IS NULL
      AND signature_sha256 IS NULL AND approved_by_operator_id IS NULL AND approved_at IS NULL)
    OR(status='published' AND signing_key_id IS NOT NULL AND signature_b64 IS NOT NULL
      AND signature_sha256 IS NOT NULL AND approved_by_operator_id IS NOT NULL
      AND approved_at IS NOT NULL)
    OR status='rejected')
);
CREATE TRIGGER trg_api_contract_snapshots_update_prd133
BEFORE UPDATE ON api_contract_snapshots FOR EACH ROW
WHEN NEW.api_contract_snapshot_id!=OLD.api_contract_snapshot_id
  OR NEW.api_version_id!=OLD.api_version_id OR NEW.public_registry_json!=OLD.public_registry_json
  OR NEW.canonical_sha256!=OLD.canonical_sha256
  OR NEW.compatibility_baseline_sha256 IS NOT OLD.compatibility_baseline_sha256
  OR NEW.compatibility_outcome!=OLD.compatibility_outcome
  OR NEW.breaking_reasons_json!=OLD.breaking_reasons_json
  OR NEW.producer_revision!=OLD.producer_revision OR NEW.source_sha256!=OLD.source_sha256
  OR NEW.build_sha256!=OLD.build_sha256 OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1
  OR NOT(OLD.status='pending_approval' AND NEW.status IN('published','rejected'))
BEGIN SELECT RAISE(ABORT,'api_contract_snapshot_transition_invalid'); END;
CREATE TRIGGER trg_api_contract_snapshots_delete_prd133 BEFORE DELETE ON api_contract_snapshots
BEGIN SELECT RAISE(ABORT,'api_contract_snapshot_immutable'); END;

CREATE TABLE sdk_releases(
  sdk_release_id TEXT PRIMARY KEY CHECK(length(sdk_release_id)=36
    AND substr(sdk_release_id,1,4)='sdr_' AND substr(sdk_release_id,5) NOT GLOB '*[^0-9a-f]*'),
  api_contract_snapshot_id TEXT NOT NULL,
  language TEXT NOT NULL CHECK(language IN('python','javascript')),
  sdk_version TEXT NOT NULL CHECK(sdk_version GLOB '[0-9]*.[0-9]*.[0-9]*' AND length(sdk_version)<=32),
  runtime_version TEXT NOT NULL CHECK(length(runtime_version) BETWEEN 3 AND 80),
  contract_sha256 TEXT NOT NULL CHECK(length(contract_sha256)=64),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  build_sha256 TEXT NOT NULL CHECK(length(build_sha256)=64),
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  sbom_sha256 TEXT NOT NULL CHECK(length(sbom_sha256)=64),
  provenance_sha256 TEXT NOT NULL CHECK(length(provenance_sha256)=64),
  file_manifest_json TEXT NOT NULL CHECK(json_valid(file_manifest_json)),
  smoke_matrix_json TEXT NOT NULL CHECK(json_valid(smoke_matrix_json)),
  artifact_receipt_sha256 TEXT NOT NULL CHECK(length(artifact_receipt_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('pending_approval','published','withdrawn')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  signing_key_id TEXT,
  signature_b64 TEXT CHECK(signature_b64 IS NULL OR length(signature_b64)=86),
  signature_sha256 TEXT CHECK(signature_sha256 IS NULL OR length(signature_sha256)=64),
  approved_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  published_at TEXT CHECK(published_at IS NULL OR unixepoch(published_at) IS NOT NULL),
  FOREIGN KEY(api_contract_snapshot_id)
    REFERENCES api_contract_snapshots(api_contract_snapshot_id),
  FOREIGN KEY(signing_key_id) REFERENCES developer_contract_signing_keys(key_id),
  FOREIGN KEY(approved_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(language,sdk_version),
  UNIQUE(language,artifact_sha256),
  CHECK((status='pending_approval' AND signing_key_id IS NULL AND signature_b64 IS NULL
      AND signature_sha256 IS NULL AND approved_by_operator_id IS NULL AND published_at IS NULL)
    OR(status!='pending_approval' AND signing_key_id IS NOT NULL AND signature_b64 IS NOT NULL
      AND signature_sha256 IS NOT NULL AND approved_by_operator_id IS NOT NULL
      AND published_at IS NOT NULL))
);
CREATE INDEX idx_sdk_releases_public_prd133 ON sdk_releases(language,status,published_at);
CREATE TRIGGER trg_sdk_releases_update_prd133
BEFORE UPDATE ON sdk_releases FOR EACH ROW
WHEN NEW.sdk_release_id!=OLD.sdk_release_id
  OR NEW.api_contract_snapshot_id!=OLD.api_contract_snapshot_id OR NEW.language!=OLD.language
  OR NEW.sdk_version!=OLD.sdk_version OR NEW.runtime_version!=OLD.runtime_version
  OR NEW.contract_sha256!=OLD.contract_sha256 OR NEW.source_sha256!=OLD.source_sha256
  OR NEW.build_sha256!=OLD.build_sha256 OR NEW.artifact_sha256!=OLD.artifact_sha256
  OR NEW.sbom_sha256!=OLD.sbom_sha256 OR NEW.provenance_sha256!=OLD.provenance_sha256
  OR NEW.file_manifest_json!=OLD.file_manifest_json
  OR NEW.smoke_matrix_json!=OLD.smoke_matrix_json
  OR NEW.artifact_receipt_sha256!=OLD.artifact_receipt_sha256
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NOT((OLD.status='pending_approval' AND NEW.status='published')
    OR(OLD.status='published' AND NEW.status='withdrawn'))
BEGIN SELECT RAISE(ABORT,'sdk_release_transition_invalid'); END;
CREATE TRIGGER trg_sdk_releases_delete_prd133 BEFORE DELETE ON sdk_releases
BEGIN SELECT RAISE(ABORT,'sdk_release_immutable'); END;

CREATE TABLE deprecation_notices(
  deprecation_notice_id TEXT PRIMARY KEY CHECK(length(deprecation_notice_id)=36
    AND substr(deprecation_notice_id,1,4)='dpn_'
    AND substr(deprecation_notice_id,5) NOT GLOB '*[^0-9a-f]*'),
  api_version_id TEXT NOT NULL,
  replacement_api_version_id TEXT NOT NULL,
  affected_operations_json TEXT NOT NULL CHECK(json_valid(affected_operations_json)),
  migration_guide_code TEXT NOT NULL CHECK(length(migration_guide_code) BETWEEN 3 AND 80
    AND migration_guide_code=lower(migration_guide_code)
    AND migration_guide_code NOT GLOB '*[^a-z0-9._-]*'),
  status TEXT NOT NULL CHECK(status IN(
    'draft','announced','sunset_scheduled','sunset','withdrawn')),
  minimum_notice_days INTEGER NOT NULL CHECK(minimum_notice_days>=90),
  adoption_watermark INTEGER NOT NULL CHECK(adoption_watermark>=0),
  notification_watermark INTEGER NOT NULL CHECK(notification_watermark>=0),
  sunset_at TEXT NOT NULL CHECK(unixepoch(sunset_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_operator_id TEXT NOT NULL,
  announced_by_operator_id TEXT,
  decided_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  announced_at TEXT CHECK(announced_at IS NULL OR unixepoch(announced_at) IS NOT NULL),
  decided_at TEXT CHECK(decided_at IS NULL OR unixepoch(decided_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(api_version_id) REFERENCES api_versions(api_version_id),
  FOREIGN KEY(replacement_api_version_id) REFERENCES api_versions(api_version_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(announced_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(decided_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK(api_version_id!=replacement_api_version_id),
  CHECK(created_by_operator_id!=coalesce(announced_by_operator_id,'')),
  CHECK((status='draft' AND announced_by_operator_id IS NULL AND announced_at IS NULL)
    OR(status!='draft' AND announced_by_operator_id IS NOT NULL AND announced_at IS NOT NULL)),
  CHECK((status IN('sunset','withdrawn') AND decided_by_operator_id IS NOT NULL
      AND decided_at IS NOT NULL) OR status NOT IN('sunset','withdrawn'))
);
CREATE INDEX idx_deprecation_notices_due_prd133
  ON deprecation_notices(status,sunset_at,deprecation_notice_id);
CREATE TRIGGER trg_deprecation_notices_update_prd133
BEFORE UPDATE ON deprecation_notices FOR EACH ROW
WHEN NEW.deprecation_notice_id!=OLD.deprecation_notice_id
  OR NEW.api_version_id!=OLD.api_version_id
  OR NEW.replacement_api_version_id!=OLD.replacement_api_version_id
  OR NEW.affected_operations_json!=OLD.affected_operations_json
  OR NEW.migration_guide_code!=OLD.migration_guide_code
  OR NEW.minimum_notice_days!=OLD.minimum_notice_days
  OR NEW.sunset_at!=OLD.sunset_at OR NEW.created_by_operator_id!=OLD.created_by_operator_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='draft' AND NEW.status='announced')
    OR(OLD.status='announced' AND NEW.status IN('sunset_scheduled','withdrawn'))
    OR(OLD.status='sunset_scheduled' AND NEW.status IN('sunset','withdrawn')))
BEGIN SELECT RAISE(ABORT,'deprecation_notice_transition_invalid'); END;
CREATE TRIGGER trg_deprecation_notices_delete_prd133 BEFORE DELETE ON deprecation_notices
BEGIN SELECT RAISE(ABORT,'deprecation_notice_immutable'); END;

CREATE TABLE webhook_schema_versions(
  webhook_schema_version_id TEXT PRIMARY KEY CHECK(length(webhook_schema_version_id)=36
    AND substr(webhook_schema_version_id,1,4)='wsv_'
    AND substr(webhook_schema_version_id,5) NOT GLOB '*[^0-9a-f]*'),
  api_contract_snapshot_id TEXT NOT NULL,
  event_name TEXT NOT NULL CHECK(length(event_name) BETWEEN 3 AND 120
    AND event_name=lower(event_name) AND event_name NOT GLOB '*[^a-z0-9._-]*'),
  schema_version INTEGER NOT NULL CHECK(schema_version>=1),
  schema_json TEXT NOT NULL CHECK(json_valid(schema_json) AND length(schema_json)<=65536),
  schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256)=64),
  compatibility_outcome TEXT NOT NULL CHECK(compatibility_outcome='compatible'),
  sample_sha256 TEXT NOT NULL CHECK(length(sample_sha256)=64),
  published_at TEXT NOT NULL CHECK(unixepoch(published_at) IS NOT NULL),
  FOREIGN KEY(api_contract_snapshot_id)
    REFERENCES api_contract_snapshots(api_contract_snapshot_id),
  UNIQUE(event_name,schema_version)
);
CREATE TRIGGER trg_webhook_schema_versions_update_prd133 BEFORE UPDATE ON webhook_schema_versions
BEGIN SELECT RAISE(ABORT,'webhook_schema_version_immutable'); END;
CREATE TRIGGER trg_webhook_schema_versions_delete_prd133 BEFORE DELETE ON webhook_schema_versions
BEGIN SELECT RAISE(ABORT,'webhook_schema_version_immutable'); END;

CREATE TABLE developer_contract_download_audits(
  developer_contract_download_audit_id TEXT PRIMARY KEY CHECK(
    length(developer_contract_download_audit_id)=36
    AND substr(developer_contract_download_audit_id,1,4)='dca_'
    AND substr(developer_contract_download_audit_id,5) NOT GLOB '*[^0-9a-f]*'),
  sdk_release_id TEXT NOT NULL,
  consumer_tenant_sha256 TEXT NOT NULL CHECK(length(consumer_tenant_sha256)=64),
  consumer_user_sha256 TEXT NOT NULL CHECK(length(consumer_user_sha256)=64),
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  downloaded_at TEXT NOT NULL CHECK(unixepoch(downloaded_at) IS NOT NULL),
  FOREIGN KEY(sdk_release_id) REFERENCES sdk_releases(sdk_release_id)
);
CREATE TRIGGER trg_developer_contract_downloads_update_prd133
BEFORE UPDATE ON developer_contract_download_audits
BEGIN SELECT RAISE(ABORT,'developer_contract_download_immutable'); END;
CREATE TRIGGER trg_developer_contract_downloads_delete_prd133
BEFORE DELETE ON developer_contract_download_audits
BEGIN SELECT RAISE(ABORT,'developer_contract_download_immutable'); END;

CREATE TABLE developer_contract_events(
  developer_contract_event_id TEXT PRIMARY KEY CHECK(length(developer_contract_event_id)=36
    AND substr(developer_contract_event_id,1,4)='dce_'
    AND substr(developer_contract_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'api.deprecated','api.sunset_scheduled','sdk.released','webhook.schema_published')),
  source_object_type TEXT NOT NULL CHECK(source_object_type IN(
    'api_version','deprecation_notice','sdk_release','webhook_schema_version')),
  source_object_id TEXT NOT NULL CHECK(length(source_object_id)=36),
  source_revision INTEGER NOT NULL CHECK(source_revision>=1),
  safe_metadata_json TEXT NOT NULL CHECK(json_valid(safe_metadata_json)),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  UNIQUE(event_type,source_object_id,source_revision)
);
CREATE TRIGGER trg_developer_contract_events_update_prd133 BEFORE UPDATE ON developer_contract_events
BEGIN SELECT RAISE(ABORT,'developer_contract_event_immutable'); END;
CREATE TRIGGER trg_developer_contract_events_delete_prd133 BEFORE DELETE ON developer_contract_events
BEGIN SELECT RAISE(ABORT,'developer_contract_event_immutable'); END;

CREATE TABLE developer_contract_event_outbox(
  developer_contract_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(developer_contract_event_outbox_id)=36
    AND substr(developer_contract_event_outbox_id,1,4)='dco_'
    AND substr(developer_contract_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  developer_contract_event_id TEXT NOT NULL,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('webhook','notification')),
  status TEXT NOT NULL CHECK(status IN('pending','delivered','dead_lettered')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  last_error_code TEXT,
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(developer_contract_event_id)
    REFERENCES developer_contract_events(developer_contract_event_id),
  UNIQUE(developer_contract_event_id,delivery_domain)
);
CREATE INDEX idx_developer_contract_outbox_due_prd133
  ON developer_contract_event_outbox(status,next_attempt_at,developer_contract_event_outbox_id);
CREATE TRIGGER trg_developer_contract_outbox_update_prd133
BEFORE UPDATE ON developer_contract_event_outbox FOR EACH ROW
WHEN NEW.developer_contract_event_outbox_id!=OLD.developer_contract_event_outbox_id
  OR NEW.developer_contract_event_id!=OLD.developer_contract_event_id
  OR NEW.delivery_domain!=OLD.delivery_domain OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count<OLD.attempt_count OR NEW.updated_at<=OLD.updated_at
  OR NOT((NEW.status=OLD.status) OR(OLD.status='pending'
    AND NEW.status IN('delivered','dead_lettered')))
BEGIN SELECT RAISE(ABORT,'developer_contract_outbox_transition_invalid'); END;
CREATE TRIGGER trg_developer_contract_outbox_delete_prd133
BEFORE DELETE ON developer_contract_event_outbox
BEGIN SELECT RAISE(ABORT,'developer_contract_outbox_immutable'); END;

CREATE TABLE developer_contract_action_commands(
  developer_contract_action_command_id TEXT PRIMARY KEY CHECK(
    length(developer_contract_action_command_id)=36
    AND substr(developer_contract_action_command_id,1,4)='dcc_'
    AND substr(developer_contract_action_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  actor_operator_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'contract.build','contract.approve','deprecation.create','deprecation.announce',
    'deprecation.withdraw','deprecation.sunset','sdk.withdraw')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(actor_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(actor_operator_id,action_code,idempotency_sha256)
);
CREATE TRIGGER trg_developer_contract_actions_update_prd133
BEFORE UPDATE ON developer_contract_action_commands
BEGIN SELECT RAISE(ABORT,'developer_contract_action_immutable'); END;
CREATE TRIGGER trg_developer_contract_actions_delete_prd133
BEFORE DELETE ON developer_contract_action_commands
BEGIN SELECT RAISE(ABORT,'developer_contract_action_immutable'); END;

CREATE TABLE developer_contract_scheduler_state(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  scan_watermark TEXT NOT NULL CHECK(unixepoch(scan_watermark) IS NOT NULL),
  lease_owner_id TEXT,
  lease_token_sha256 TEXT CHECK(lease_token_sha256 IS NULL OR length(lease_token_sha256)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK((lease_owner_id IS NULL AND lease_token_sha256 IS NULL AND lease_expires_at IS NULL)
    OR(lease_owner_id IS NOT NULL AND length(lease_owner_id) BETWEEN 8 AND 128
      AND lease_token_sha256 IS NOT NULL AND lease_expires_at IS NOT NULL))
);
INSERT INTO developer_contract_scheduler_state VALUES(
  1,'1970-01-01T00:00:00.000Z',NULL,NULL,NULL,0,1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_developer_contract_scheduler_update_prd133
BEFORE UPDATE ON developer_contract_scheduler_state FOR EACH ROW
WHEN NEW.singleton_id!=OLD.singleton_id OR NEW.fencing_generation<OLD.fencing_generation
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'developer_contract_scheduler_fencing_invalid'); END;
CREATE TRIGGER trg_developer_contract_scheduler_delete_prd133
BEFORE DELETE ON developer_contract_scheduler_state
BEGIN SELECT RAISE(ABORT,'developer_contract_scheduler_required'); END;

CREATE TEMP TABLE prd133_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd133_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_developer_contract_permission_extensions)=3
      AND (SELECT COUNT(*) FROM platform_developer_contract_role_permissions)=6
      AND (SELECT COUNT(*) FROM developer_contract_scheduler_state)=1
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd133_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
