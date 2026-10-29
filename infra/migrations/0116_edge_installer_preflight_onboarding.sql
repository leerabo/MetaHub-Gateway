-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd124_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd124_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0115' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'edge_deployments','workspace_edge_permission_extensions',
      'workspace_edge_secret_permission_extensions','platform_edge_permission_extensions',
      'tenant_deletion_table_manifest','tenant_deletion_trigger_manifest'))=6
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd124')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd124_up_guard;

CREATE TABLE workspace_edge_installer_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'edge.installer.read','edge.installation.manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-124'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=34)
);
CREATE TABLE workspace_edge_installer_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_edge_installer_permission_extensions(permission_code)
);
INSERT INTO workspace_edge_installer_permission_extensions VALUES
  ('edge.installer.read','PRD-124',34),
  ('edge.installation.manage','PRD-124',34);
INSERT INTO workspace_edge_installer_role_permissions VALUES
  ('owner','edge.installer.read'),
  ('owner','edge.installation.manage');
CREATE TRIGGER trg_workspace_edge_installer_permissions_insert_prd124
BEFORE INSERT ON workspace_edge_installer_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_installer_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_installer_permissions_update_prd124
BEFORE UPDATE ON workspace_edge_installer_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_installer_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_installer_permissions_delete_prd124
BEFORE DELETE ON workspace_edge_installer_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_installer_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_installer_roles_insert_prd124
BEFORE INSERT ON workspace_edge_installer_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_installer_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_installer_roles_update_prd124
BEFORE UPDATE ON workspace_edge_installer_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_installer_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_installer_roles_delete_prd124
BEFORE DELETE ON workspace_edge_installer_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_installer_permission_immutable'); END;

CREATE TABLE platform_edge_installer_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.edge_installer.read','platform.edge_installer.manage',
    'platform.edge_installer.approve')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-124'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=34)
);
CREATE TABLE platform_edge_installer_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'platform_owner','platform_admin','platform_ops','platform_finance','platform_support',
    'platform_security','platform_auditor','platform_compliance','platform_privacy',
    'release_manager','platform_provider_engineer','platform_channel')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_edge_installer_permission_extensions(permission_code)
);
INSERT INTO platform_edge_installer_permission_extensions VALUES
  ('platform.edge_installer.read','PRD-124',34),
  ('platform.edge_installer.manage','PRD-124',34),
  ('platform.edge_installer.approve','PRD-124',34);
INSERT INTO platform_edge_installer_role_permissions VALUES
  ('platform_owner','platform.edge_installer.read'),
  ('platform_security','platform.edge_installer.read'),
  ('release_manager','platform.edge_installer.read'),
  ('release_manager','platform.edge_installer.manage'),
  ('release_manager','platform.edge_installer.approve');
CREATE TRIGGER trg_platform_edge_installer_permissions_insert_prd124
BEFORE INSERT ON platform_edge_installer_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_installer_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_installer_permissions_update_prd124
BEFORE UPDATE ON platform_edge_installer_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_installer_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_installer_permissions_delete_prd124
BEFORE DELETE ON platform_edge_installer_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_installer_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_installer_roles_insert_prd124
BEFORE INSERT ON platform_edge_installer_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_installer_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_installer_roles_update_prd124
BEFORE UPDATE ON platform_edge_installer_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_installer_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_installer_roles_delete_prd124
BEFORE DELETE ON platform_edge_installer_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_installer_permission_immutable'); END;

CREATE TABLE installer_trusted_release_keys(
  key_id TEXT PRIMARY KEY CHECK(length(key_id) BETWEEN 3 AND 80
    AND key_id=lower(key_id) AND key_id NOT GLOB '*[^a-z0-9._:-]*'),
  algorithm TEXT NOT NULL CHECK(algorithm='Ed25519'),
  public_key_b64 TEXT NOT NULL UNIQUE CHECK(length(public_key_b64)=43
    AND public_key_b64 NOT GLOB '*[^A-Za-z0-9_-]*'),
  signature_purpose TEXT NOT NULL CHECK(
    signature_purpose='metahub.edge-installer.release/v1'),
  status TEXT NOT NULL CHECK(status IN('active','retired')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
INSERT INTO installer_trusted_release_keys VALUES(
  'metahub-installer-release-v1','Ed25519',
  'iWdr6OXtZwavcXTcBc7NNFybeHG8C5iieyizQx1vdA8',
  'metahub.edge-installer.release/v1','active','2026-08-27T00:00:00.000Z');
CREATE TRIGGER trg_installer_trusted_keys_insert_prd124
BEFORE INSERT ON installer_trusted_release_keys
BEGIN SELECT RAISE(ABORT,'installer_trusted_key_immutable'); END;
CREATE TRIGGER trg_installer_trusted_keys_update_prd124
BEFORE UPDATE ON installer_trusted_release_keys
BEGIN SELECT RAISE(ABORT,'installer_trusted_key_immutable'); END;
CREATE TRIGGER trg_installer_trusted_keys_delete_prd124
BEFORE DELETE ON installer_trusted_release_keys
BEGIN SELECT RAISE(ABORT,'installer_trusted_key_immutable'); END;

CREATE TABLE installer_releases(
  installer_release_id TEXT PRIMARY KEY CHECK(length(installer_release_id)=36
    AND substr(installer_release_id,1,4)='irl_'
    AND substr(installer_release_id,5) NOT GLOB '*[^0-9a-f]*'),
  version TEXT NOT NULL UNIQUE CHECK(length(version) BETWEEN 5 AND 32
    AND version NOT GLOB '*[^0-9.]*'),
  channel TEXT NOT NULL CHECK(channel IN('stable','extended')),
  status TEXT NOT NULL CHECK(status IN('approval_pending','released','withdrawn')),
  manifest_sha256 TEXT NOT NULL UNIQUE CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  sbom_sha256 TEXT NOT NULL CHECK(length(sbom_sha256)=64
    AND sbom_sha256 NOT GLOB '*[^0-9a-f]*'),
  signature_algorithm TEXT NOT NULL CHECK(signature_algorithm='Ed25519'),
  signature_key_id TEXT NOT NULL,
  signature_value TEXT NOT NULL CHECK(length(signature_value)=86
    AND signature_value NOT GLOB '*[^A-Za-z0-9_-]*'),
  signature_purpose TEXT NOT NULL CHECK(
    signature_purpose='metahub.edge-installer.release/v1'),
  supported_os_json TEXT NOT NULL CHECK(supported_os_json='["linux"]'),
  supported_arch_json TEXT NOT NULL CHECK(
    supported_arch_json='["aarch64","x86_64"]'),
  schema_min INTEGER NOT NULL CHECK(schema_min BETWEEN 1 AND 9999),
  schema_max INTEGER NOT NULL CHECK(schema_max BETWEEN 1 AND 9999),
  agent_min_version TEXT NOT NULL CHECK(length(agent_min_version) BETWEEN 5 AND 32),
  agent_max_version TEXT NOT NULL CHECK(length(agent_max_version) BETWEEN 5 AND 32),
  control_min_version TEXT NOT NULL CHECK(length(control_min_version) BETWEEN 5 AND 32),
  control_max_version TEXT NOT NULL CHECK(length(control_max_version) BETWEEN 5 AND 32),
  n_minus_one INTEGER NOT NULL CHECK(n_minus_one=1),
  revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
  submitted_by_account_id TEXT NOT NULL CHECK(length(submitted_by_account_id) BETWEEN 3 AND 160),
  approved_by_account_id TEXT CHECK(approved_by_account_id IS NULL
    OR length(approved_by_account_id) BETWEEN 3 AND 160),
  submitted_at TEXT NOT NULL CHECK(unixepoch(submitted_at) IS NOT NULL),
  approved_at TEXT CHECK(approved_at IS NULL OR unixepoch(approved_at) IS NOT NULL),
  withdrawn_at TEXT CHECK(withdrawn_at IS NULL OR unixepoch(withdrawn_at) IS NOT NULL),
  FOREIGN KEY(signature_key_id) REFERENCES installer_trusted_release_keys(key_id),
  CHECK(schema_min<=schema_max),
  CHECK((status='approval_pending' AND approved_by_account_id IS NULL
      AND approved_at IS NULL AND withdrawn_at IS NULL)
    OR(status='released' AND approved_by_account_id IS NOT NULL
      AND approved_by_account_id!=submitted_by_account_id
      AND approved_at IS NOT NULL AND withdrawn_at IS NULL)
    OR(status='withdrawn' AND withdrawn_at IS NOT NULL))
);
INSERT INTO installer_releases VALUES(
  'irl_12400000000000000000000000000000','1.0.0','stable','released',
  '430dea749d487ce6a3aee1a722e43f202f2a77ce199eca6837e930813598d587',
  '147f2ce8260f83fc22d10e8c0b29dffd0be66e9efa20d0c85685f0d78629492f',
  'Ed25519','metahub-installer-release-v1',
  'ETcoV9HjVqKMDuevJyw3rCs6fwaFTWkblRxcJh6w9XUKR3bRY2598UgpfbMMR2Cbl3_d87SvVvEOYvRs_mI8AQ',
  'metahub.edge-installer.release/v1','["linux"]','["aarch64","x86_64"]',
  115,116,'1.0.0','1.1.99','1.0.0','1.1.99',1,1,
  'system-release-builder','system-release-approver',
  '2026-08-27T00:00:00.000Z','2026-08-27T00:00:00.000Z',NULL);
CREATE TRIGGER trg_installer_releases_insert_prd124
BEFORE INSERT ON installer_releases FOR EACH ROW
WHEN NEW.status!='approval_pending' OR NEW.revision!=1
BEGIN SELECT RAISE(ABORT,'installer_release_insert_invalid'); END;
CREATE TRIGGER trg_installer_releases_update_prd124
BEFORE UPDATE ON installer_releases FOR EACH ROW
WHEN NEW.installer_release_id!=OLD.installer_release_id OR NEW.version!=OLD.version
  OR NEW.channel!=OLD.channel OR NEW.manifest_sha256!=OLD.manifest_sha256
  OR NEW.sbom_sha256!=OLD.sbom_sha256
  OR NEW.signature_algorithm!=OLD.signature_algorithm
  OR NEW.signature_key_id!=OLD.signature_key_id
  OR NEW.signature_value!=OLD.signature_value
  OR NEW.signature_purpose!=OLD.signature_purpose
  OR NEW.supported_os_json!=OLD.supported_os_json
  OR NEW.supported_arch_json!=OLD.supported_arch_json
  OR NEW.schema_min!=OLD.schema_min OR NEW.schema_max!=OLD.schema_max
  OR NEW.agent_min_version!=OLD.agent_min_version
  OR NEW.agent_max_version!=OLD.agent_max_version
  OR NEW.control_min_version!=OLD.control_min_version
  OR NEW.control_max_version!=OLD.control_max_version
  OR NEW.n_minus_one!=OLD.n_minus_one
  OR NEW.submitted_by_account_id!=OLD.submitted_by_account_id
  OR NEW.submitted_at!=OLD.submitted_at OR NEW.revision!=OLD.revision+1
  OR (OLD.status='approval_pending' AND NEW.status NOT IN('released','withdrawn'))
  OR (OLD.status='released' AND NEW.status!='withdrawn')
  OR OLD.status='withdrawn'
BEGIN SELECT RAISE(ABORT,'installer_release_transition_invalid'); END;
CREATE TRIGGER trg_installer_releases_delete_prd124
BEFORE DELETE ON installer_releases
BEGIN SELECT RAISE(ABORT,'installer_release_immutable'); END;

CREATE TABLE installer_release_artifacts(
  installer_artifact_id TEXT PRIMARY KEY CHECK(length(installer_artifact_id)=36
    AND substr(installer_artifact_id,1,4)='ira_'
    AND substr(installer_artifact_id,5) NOT GLOB '*[^0-9a-f]*'),
  installer_release_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN('cli','compose','container_bundle','profile','sbom')),
  filename TEXT NOT NULL CHECK(length(filename) BETWEEN 1 AND 120
    AND filename NOT GLOB '*[/\\]*' AND filename NOT IN('.','..')),
  media_type TEXT NOT NULL CHECK(length(media_type) BETWEEN 3 AND 96),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64
    AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
  sbom_sha256 TEXT NOT NULL CHECK(length(sbom_sha256)=64
    AND sbom_sha256 NOT GLOB '*[^0-9a-f]*'),
  size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 8589934592),
  executable INTEGER NOT NULL CHECK(executable IN(0,1)),
  download_path TEXT NOT NULL CHECK(length(download_path) BETWEEN 32 AND 240
    AND download_path GLOB '/edge/installers/*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(installer_release_id) REFERENCES installer_releases(installer_release_id),
  UNIQUE(installer_release_id,installer_artifact_id),
  UNIQUE(installer_release_id,filename)
);
INSERT INTO installer_release_artifacts VALUES
  ('ira_12400000000000000000000000000001','irl_12400000000000000000000000000000',
    'cli','metahub-installer.py','text/x-python',
    'a489a90a6a34a75a91c5eabec230a7a900e60cf08235ab17ee572f7383a1d613',
    '147f2ce8260f83fc22d10e8c0b29dffd0be66e9efa20d0c85685f0d78629492f',201,1,
    '/edge/installers/irl_12400000000000000000000000000000/artifacts/ira_12400000000000000000000000000001',
    '2026-08-27T00:00:00.000Z'),
  ('ira_12400000000000000000000000000002','irl_12400000000000000000000000000000',
    'compose','compose.yaml','application/yaml',
    'b239c689b71397b143f6cfaf11e84c33de04276fcca7fb8e86a444fffc3517c5',
    '147f2ce8260f83fc22d10e8c0b29dffd0be66e9efa20d0c85685f0d78629492f',1575,0,
    '/edge/installers/irl_12400000000000000000000000000000/artifacts/ira_12400000000000000000000000000002',
    '2026-08-27T00:00:00.000Z'),
  ('ira_12400000000000000000000000000003','irl_12400000000000000000000000000000',
    'container_bundle','container-images.json',
    'application/vnd.docker.distribution.manifest.v2+json',
    '6b997c3d0f752bca53e37ee264c2b72aed8e250a1114bb9796073ab2bf7956c9',
    '147f2ce8260f83fc22d10e8c0b29dffd0be66e9efa20d0c85685f0d78629492f',448,0,
    '/edge/installers/irl_12400000000000000000000000000000/artifacts/ira_12400000000000000000000000000003',
    '2026-08-27T00:00:00.000Z'),
  ('ira_12400000000000000000000000000004','irl_12400000000000000000000000000000',
    'profile','profiles.json','application/json',
    'd5a6f8ae5af6a5b137dd8d8006dcc959cbdc4a2bd865e342045d600e1e63791e',
    '147f2ce8260f83fc22d10e8c0b29dffd0be66e9efa20d0c85685f0d78629492f',1142,0,
    '/edge/installers/irl_12400000000000000000000000000000/artifacts/ira_12400000000000000000000000000004',
    '2026-08-27T00:00:00.000Z'),
  ('ira_12400000000000000000000000000005','irl_12400000000000000000000000000000',
    'sbom','sbom.spdx.json','application/spdx+json',
    '147f2ce8260f83fc22d10e8c0b29dffd0be66e9efa20d0c85685f0d78629492f',
    '147f2ce8260f83fc22d10e8c0b29dffd0be66e9efa20d0c85685f0d78629492f',985,0,
    '/edge/installers/irl_12400000000000000000000000000000/artifacts/ira_12400000000000000000000000000005',
    '2026-08-27T00:00:00.000Z');
CREATE TRIGGER trg_installer_artifacts_insert_prd124
BEFORE INSERT ON installer_release_artifacts FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM installer_releases release
  WHERE release.installer_release_id=NEW.installer_release_id
    AND release.status='approval_pending')
BEGIN SELECT RAISE(ABORT,'installer_artifact_immutable'); END;
CREATE TRIGGER trg_installer_artifacts_update_prd124
BEFORE UPDATE ON installer_release_artifacts
BEGIN SELECT RAISE(ABORT,'installer_artifact_immutable'); END;
CREATE TRIGGER trg_installer_artifacts_delete_prd124
BEFORE DELETE ON installer_release_artifacts
BEGIN SELECT RAISE(ABORT,'installer_artifact_immutable'); END;

CREATE TABLE installer_profiles(
  install_profile_id TEXT PRIMARY KEY CHECK(length(install_profile_id)=36
    AND substr(install_profile_id,1,4)='ipr_'
    AND substr(install_profile_id,5) NOT GLOB '*[^0-9a-f]*'),
  installer_release_id TEXT NOT NULL,
  profile_code TEXT NOT NULL CHECK(profile_code IN('compact','standard')),
  display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 2 AND 80),
  cpu_cores INTEGER NOT NULL CHECK(cpu_cores BETWEEN 2 AND 256),
  memory_mib INTEGER NOT NULL CHECK(memory_mib BETWEEN 2048 AND 1048576),
  disk_mib INTEGER NOT NULL CHECK(disk_mib BETWEEN 10240 AND 16777216),
  required_ports_json TEXT NOT NULL CHECK(json_valid(required_ports_json)
    AND json_type(required_ports_json)='array'
    AND json_array_length(required_ports_json) BETWEEN 1 AND 16
    AND length(required_ports_json)<=128),
  dns_targets_json TEXT NOT NULL CHECK(json_valid(dns_targets_json)
    AND json_type(dns_targets_json)='array' AND length(dns_targets_json)<=512),
  runtime TEXT NOT NULL CHECK(runtime='docker'),
  runtime_min_version TEXT NOT NULL CHECK(length(runtime_min_version) BETWEEN 5 AND 32),
  filesystem TEXT NOT NULL CHECK(filesystem='local'),
  proxy_modes_json TEXT NOT NULL CHECK(proxy_modes_json IN(
    '["direct"]','["http_connect"]','["direct","http_connect"]')),
  ca_mode TEXT NOT NULL CHECK(ca_mode IN('system','system_or_custom')),
  revision INTEGER NOT NULL CHECK(revision=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(installer_release_id) REFERENCES installer_releases(installer_release_id),
  UNIQUE(installer_release_id,install_profile_id),
  UNIQUE(installer_release_id,profile_code)
);
INSERT INTO installer_profiles VALUES
  ('ipr_12400000000000000000000000000001','irl_12400000000000000000000000000000',
    'compact','Compact private gateway',2,2048,10240,'[443,8000,8090]',
    '["api.metahub.example","registry.metahub.example"]','docker','24.0.0','local',
    '["direct","http_connect"]','system_or_custom',1,'2026-08-27T00:00:00.000Z'),
  ('ipr_12400000000000000000000000000002','irl_12400000000000000000000000000000',
    'standard','Standard private gateway',4,8192,40960,'[443,8000,8090]',
    '["api.metahub.example","registry.metahub.example"]','docker','24.0.0','local',
    '["direct","http_connect"]','system_or_custom',1,'2026-08-27T00:00:00.000Z');
CREATE TRIGGER trg_installer_profiles_insert_prd124
BEFORE INSERT ON installer_profiles FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM installer_releases release
  WHERE release.installer_release_id=NEW.installer_release_id
    AND release.status='approval_pending')
BEGIN SELECT RAISE(ABORT,'installer_profile_immutable'); END;
CREATE TRIGGER trg_installer_profiles_update_prd124
BEFORE UPDATE ON installer_profiles
BEGIN SELECT RAISE(ABORT,'installer_profile_immutable'); END;
CREATE TRIGGER trg_installer_profiles_delete_prd124
BEFORE DELETE ON installer_profiles
BEGIN SELECT RAISE(ABORT,'installer_profile_immutable'); END;

CREATE TABLE platform_installer_release_commands(
  release_command_id TEXT PRIMARY KEY CHECK(length(release_command_id)=36
    AND substr(release_command_id,1,4)='irc_'
    AND substr(release_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  operator_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'platform.edge_installer.submit','platform.edge_installer.approve',
    'platform.edge_installer.withdraw')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  installer_release_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(installer_release_id) REFERENCES installer_releases(installer_release_id),
  UNIQUE(operator_id,action_code,idempotency_sha256)
);
CREATE TRIGGER trg_platform_installer_commands_update_prd124
BEFORE UPDATE ON platform_installer_release_commands
BEGIN SELECT RAISE(ABORT,'platform_installer_command_immutable'); END;
CREATE TRIGGER trg_platform_installer_commands_delete_prd124
BEFORE DELETE ON platform_installer_release_commands
BEGIN SELECT RAISE(ABORT,'platform_installer_command_immutable'); END;

CREATE TABLE edge_preflight_reports(
  preflight_report_id TEXT PRIMARY KEY CHECK(length(preflight_report_id)=36
    AND substr(preflight_report_id,1,4)='pfr_'
    AND substr(preflight_report_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  installer_release_id TEXT NOT NULL,
  install_profile_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND 2147483647),
  outcome TEXT NOT NULL CHECK(outcome IN('passed','failed')),
  environment_sha256 TEXT NOT NULL CHECK(length(environment_sha256)=64
    AND environment_sha256 NOT GLOB '*[^0-9a-f]*'),
  checks_json TEXT NOT NULL CHECK(json_valid(checks_json) AND json_type(checks_json)='array'
    AND json_array_length(checks_json) BETWEEN 10 AND 32 AND length(checks_json)<=8192
    AND lower(checks_json) NOT LIKE '%hostname%'
    AND lower(checks_json) NOT LIKE '%certificate%'
    AND lower(checks_json) NOT LIKE '%environment%'
    AND lower(checks_json) NOT LIKE '%path%'),
  report_sha256 TEXT NOT NULL UNIQUE CHECK(length(report_sha256)=64
    AND report_sha256 NOT GLOB '*[^0-9a-f]*'),
  duration_ms INTEGER NOT NULL CHECK(duration_ms BETWEEN 0 AND 120000),
  started_at TEXT NOT NULL CHECK(unixepoch(started_at) IS NOT NULL),
  completed_at TEXT NOT NULL CHECK(unixepoch(completed_at) IS NOT NULL),
  received_at TEXT NOT NULL CHECK(unixepoch(received_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(installer_release_id,install_profile_id)
    REFERENCES installer_profiles(installer_release_id,install_profile_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,generation),
  UNIQUE(workspace_id,tenant_id,preflight_report_id),
  CHECK(unixepoch(completed_at)>=unixepoch(started_at))
);
CREATE INDEX idx_edge_preflight_owner_prd124
  ON edge_preflight_reports(workspace_id,tenant_id,edge_deployment_id,generation);
CREATE TRIGGER trg_edge_preflight_reports_update_prd124
BEFORE UPDATE ON edge_preflight_reports
BEGIN SELECT RAISE(ABORT,'edge_preflight_report_immutable'); END;
CREATE TRIGGER trg_edge_preflight_reports_delete_prd124
BEFORE DELETE ON edge_preflight_reports
BEGIN SELECT RAISE(ABORT,'edge_preflight_report_immutable'); END;

CREATE TABLE edge_installations(
  installation_id TEXT PRIMARY KEY CHECK(length(installation_id)=36
    AND substr(installation_id,1,4)='ins_'
    AND substr(installation_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  installer_release_id TEXT NOT NULL,
  install_profile_id TEXT NOT NULL,
  preflight_report_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND 2147483647),
  status TEXT NOT NULL CHECK(status IN(
    'preflight_passed','installing','installed','connecting','active','failed',
    'uninstalling','uninstalled')),
  current_step TEXT CHECK(current_step IS NULL OR current_step IN(
    'staging_prepared','artifacts_copied','runtime_validated','runtime_started',
    'health_verified','release_committed','device_registered','initial_snapshot')),
  revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
  journal_sha256 TEXT NOT NULL CHECK(length(journal_sha256)=64
    AND journal_sha256 NOT GLOB '*[^0-9a-f]*'),
  last_receipt_sha256 TEXT CHECK(last_receipt_sha256 IS NULL OR(
    length(last_receipt_sha256)=64 AND last_receipt_sha256 NOT GLOB '*[^0-9a-f]*')),
  compensation_status TEXT NOT NULL CHECK(compensation_status IN(
    'not_required','pending','completed','failed')),
  retain_config INTEGER NOT NULL CHECK(retain_config IN(0,1)),
  retain_audit INTEGER NOT NULL CHECK(retain_audit IN(0,1)),
  retain_secrets INTEGER NOT NULL CHECK(retain_secrets IN(0,1)),
  failure_reason_code TEXT CHECK(failure_reason_code IS NULL OR(
    length(failure_reason_code) BETWEEN 2 AND 80
    AND failure_reason_code=lower(failure_reason_code)
    AND failure_reason_code NOT GLOB '*[^a-z0-9_.-]*')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  started_at TEXT NOT NULL CHECK(unixepoch(started_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  failed_at TEXT CHECK(failed_at IS NULL OR unixepoch(failed_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(installer_release_id,install_profile_id)
    REFERENCES installer_profiles(installer_release_id,install_profile_id),
  FOREIGN KEY(workspace_id,tenant_id,preflight_report_id)
    REFERENCES edge_preflight_reports(workspace_id,tenant_id,preflight_report_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,generation),
  UNIQUE(workspace_id,tenant_id,installation_id),
  UNIQUE(workspace_id,tenant_id,idempotency_sha256),
  CHECK((status='failed' AND failed_at IS NOT NULL AND failure_reason_code IS NOT NULL)
    OR(status!='failed' AND failed_at IS NULL AND failure_reason_code IS NULL)),
  CHECK((status IN('active','uninstalled') AND completed_at IS NOT NULL)
    OR status NOT IN('active','uninstalled'))
);
CREATE INDEX idx_edge_installations_owner_prd124
  ON edge_installations(workspace_id,tenant_id,edge_deployment_id,status,updated_at);
CREATE TRIGGER trg_edge_installations_update_prd124
BEFORE UPDATE ON edge_installations FOR EACH ROW
WHEN NEW.installation_id!=OLD.installation_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.installer_release_id!=OLD.installer_release_id
  OR NEW.install_profile_id!=OLD.install_profile_id
  OR NEW.preflight_report_id!=OLD.preflight_report_id OR NEW.generation!=OLD.generation
  OR NEW.idempotency_sha256!=OLD.idempotency_sha256
  OR NEW.request_sha256!=OLD.request_sha256 OR NEW.started_at!=OLD.started_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR (OLD.status='preflight_passed' AND NEW.status NOT IN('installing','failed'))
  OR (OLD.status='installing' AND NEW.status NOT IN('installing','installed','failed'))
  OR (OLD.status='installed' AND NEW.status NOT IN('connecting','uninstalling','failed'))
  OR (OLD.status='connecting' AND NEW.status NOT IN('connecting','active','failed'))
  OR (OLD.status='active' AND NEW.status NOT IN('uninstalling','failed'))
  OR (OLD.status='failed' AND NEW.status NOT IN('installing','connecting','uninstalling'))
  OR (OLD.status='uninstalling' AND NEW.status NOT IN('uninstalled','failed'))
  OR OLD.status='uninstalled'
BEGIN SELECT RAISE(ABORT,'edge_installation_transition_invalid'); END;
CREATE TRIGGER trg_edge_installations_delete_prd124
BEFORE DELETE ON edge_installations
BEGIN SELECT RAISE(ABORT,'edge_installation_immutable'); END;

CREATE TABLE edge_install_audits(
  install_audit_id TEXT PRIMARY KEY CHECK(length(install_audit_id)=36
    AND substr(install_audit_id,1,4)='ina_'
    AND substr(install_audit_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  installation_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND 2147483647),
  actor_type TEXT NOT NULL CHECK(actor_type IN('human','agent','system')),
  actor_account_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'edge.install_started','edge.install_completed','edge.install_failed')),
  step TEXT CHECK(step IS NULL OR step IN(
    'staging_prepared','artifacts_copied','runtime_validated','runtime_started',
    'health_verified','release_committed','device_registered','initial_snapshot')),
  outcome_code TEXT NOT NULL CHECK(outcome_code IN('started','succeeded','failed')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  receipt_sha256 TEXT CHECK(receipt_sha256 IS NULL OR(length(receipt_sha256)=64
    AND receipt_sha256 NOT GLOB '*[^0-9a-f]*')),
  previous_sha256 TEXT NOT NULL CHECK(length(previous_sha256)=64
    AND previous_sha256 NOT GLOB '*[^0-9a-f]*'),
  entry_sha256 TEXT NOT NULL CHECK(length(entry_sha256)=64
    AND entry_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,installation_id)
    REFERENCES edge_installations(workspace_id,tenant_id,installation_id),
  UNIQUE(workspace_id,tenant_id,install_audit_id),
  UNIQUE(installation_id,entry_sha256),
  CHECK((actor_type='human' AND actor_account_id IS NOT NULL)
    OR(actor_type!='human' AND actor_account_id IS NULL))
);
CREATE INDEX idx_edge_install_audits_owner_prd124
  ON edge_install_audits(workspace_id,tenant_id,installation_id,occurred_at);
CREATE TRIGGER trg_edge_install_audits_update_prd124
BEFORE UPDATE ON edge_install_audits
BEGIN SELECT RAISE(ABORT,'edge_install_audit_immutable'); END;
CREATE TRIGGER trg_edge_install_audits_delete_prd124
BEFORE DELETE ON edge_install_audits
BEGIN SELECT RAISE(ABORT,'edge_install_audit_immutable'); END;

CREATE TABLE edge_install_outbox(
  install_outbox_id TEXT PRIMARY KEY CHECK(length(install_outbox_id)=36
    AND substr(install_outbox_id,1,4)='ino_'
    AND substr(install_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  installation_id TEXT NOT NULL,
  install_audit_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'edge.install_started','edge.install_completed','edge.install_failed')),
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('workspace','fleet','security')),
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','dead_letter')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR(length(last_error_code) BETWEEN 2 AND 80
    AND last_error_code=lower(last_error_code)
    AND last_error_code NOT GLOB '*[^a-z0-9_.-]*')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,install_audit_id)
    REFERENCES edge_install_audits(workspace_id,tenant_id,install_audit_id),
  UNIQUE(install_audit_id,delivery_domain)
);
CREATE INDEX idx_edge_install_outbox_delivery_prd124
  ON edge_install_outbox(status,available_at,install_outbox_id);
CREATE TRIGGER trg_edge_install_outbox_update_prd124
BEFORE UPDATE ON edge_install_outbox FOR EACH ROW
WHEN NEW.install_outbox_id!=OLD.install_outbox_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.installation_id!=OLD.installation_id OR NEW.install_audit_id!=OLD.install_audit_id
  OR NEW.event_type!=OLD.event_type OR NEW.delivery_domain!=OLD.delivery_domain
  OR NEW.created_at!=OLD.created_at OR NEW.attempt_count<OLD.attempt_count
  OR NEW.updated_at<=OLD.updated_at
  OR (OLD.status='pending' AND NEW.status NOT IN('processing','dead_letter'))
  OR (OLD.status='processing' AND NEW.status NOT IN('pending','delivered','dead_letter'))
  OR OLD.status IN('delivered','dead_letter')
BEGIN SELECT RAISE(ABORT,'edge_install_outbox_transition_invalid'); END;
CREATE TRIGGER trg_edge_install_outbox_delete_prd124
BEFORE DELETE ON edge_install_outbox
BEGIN SELECT RAISE(ABORT,'edge_install_outbox_immutable'); END;

CREATE TABLE edge_install_commands(
  install_command_id TEXT PRIMARY KEY CHECK(length(install_command_id)=36
    AND substr(install_command_id,1,4)='inc_'
    AND substr(install_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_account_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'edge.preflight.record','edge.install.start','edge.install.step','edge.uninstall')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,actor_account_id,action_code,idempotency_sha256),
  UNIQUE(workspace_id,tenant_id,install_command_id)
);
CREATE TRIGGER trg_edge_install_commands_update_prd124
BEFORE UPDATE ON edge_install_commands
BEGIN SELECT RAISE(ABORT,'edge_install_command_immutable'); END;
CREATE TRIGGER trg_edge_install_commands_delete_prd124
BEFORE DELETE ON edge_install_commands
BEGIN SELECT RAISE(ABORT,'edge_install_command_immutable'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd124(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-124'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd124_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd124_manifest_sources VALUES
  ('edge_preflight_reports','delete','operational'),
  ('edge_installations','archive_then_delete','audit'),
  ('edge_install_audits','archive_then_delete','audit'),
  ('edge_install_outbox','delete','operational'),
  ('edge_install_commands','archive_then_delete','audit');
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
JOIN prd124_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd124(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-124',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd124_manifest_sources ORDER BY source_table;
DROP TABLE prd124_manifest_sources;

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
CREATE TRIGGER trg_tenant_deletion_manifest_prd124_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd124
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd124_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd124
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd124_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd124
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd124_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd124_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_edge_installer_permission_extensions)=2
      AND (SELECT COUNT(*) FROM workspace_edge_installer_role_permissions)=2
      AND (SELECT COUNT(*) FROM platform_edge_installer_permission_extensions)=3
      AND (SELECT COUNT(*) FROM platform_edge_installer_role_permissions)=5
      AND (SELECT COUNT(*) FROM installer_releases)=1
      AND (SELECT COUNT(*) FROM installer_release_artifacts)=5
      AND (SELECT COUNT(*) FROM installer_profiles)=2
      AND (SELECT COUNT(*) FROM platform_installer_release_commands)=0
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd124)=5
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_installer_permission_extensions',
      'workspace_edge_installer_role_permissions',
      'platform_edge_installer_permission_extensions',
      'platform_edge_installer_role_permissions','installer_trusted_release_keys',
      'installer_releases','installer_release_artifacts','installer_profiles',
      'platform_installer_release_commands',
      'edge_preflight_reports','edge_installations','edge_install_audits',
      'edge_install_outbox','edge_install_commands',
      'tenant_deletion_manifest_extensions_prd124'))=15 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd124_up_final_guard;
COMMIT;
