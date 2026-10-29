-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd138_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd138_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0129' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_roles','platform_operators','workspace_tenant_bindings',
      'tenant_deletion_table_manifest','tenant_deletion_trigger_manifest'))=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd138')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd138_up_guard;

CREATE TABLE platform_launch_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.launch.read','platform.launch.create','platform.launch.evidence.submit',
    'platform.launch.sign.product','platform.launch.sign.sre',
    'platform.launch.sign.security','platform.launch.sign.test',
    'platform.launch.sign.finance','platform.launch.sign.legal',
    'platform.launch.execute','platform.launch.rollback')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-138'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=50)
);
CREATE TABLE platform_launch_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'platform_owner','platform_ops','platform_security','platform_billing',
    'platform_verification','platform_compliance','release_manager')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_launch_permission_extensions(permission_code)
);
INSERT INTO platform_launch_permission_extensions VALUES
  ('platform.launch.read','PRD-138',50),
  ('platform.launch.create','PRD-138',50),
  ('platform.launch.evidence.submit','PRD-138',50),
  ('platform.launch.sign.product','PRD-138',50),
  ('platform.launch.sign.sre','PRD-138',50),
  ('platform.launch.sign.security','PRD-138',50),
  ('platform.launch.sign.test','PRD-138',50),
  ('platform.launch.sign.finance','PRD-138',50),
  ('platform.launch.sign.legal','PRD-138',50),
  ('platform.launch.execute','PRD-138',50),
  ('platform.launch.rollback','PRD-138',50);
INSERT INTO platform_launch_role_permissions VALUES
  ('platform_owner','platform.launch.read'),
  ('platform_owner','platform.launch.sign.product'),
  ('platform_owner','platform.launch.execute'),
  ('platform_owner','platform.launch.rollback'),
  ('platform_ops','platform.launch.read'),
  ('platform_ops','platform.launch.sign.sre'),
  ('platform_security','platform.launch.read'),
  ('platform_security','platform.launch.sign.security'),
  ('platform_billing','platform.launch.read'),
  ('platform_billing','platform.launch.sign.finance'),
  ('platform_verification','platform.launch.read'),
  ('platform_verification','platform.launch.evidence.submit'),
  ('platform_verification','platform.launch.sign.test'),
  ('platform_compliance','platform.launch.read'),
  ('platform_compliance','platform.launch.sign.legal'),
  ('release_manager','platform.launch.read'),
  ('release_manager','platform.launch.create');
CREATE TRIGGER trg_platform_launch_permissions_insert_prd138
BEFORE INSERT ON platform_launch_permission_extensions
BEGIN SELECT RAISE(ABORT,'launch_permission_immutable'); END;
CREATE TRIGGER trg_platform_launch_permissions_update_prd138
BEFORE UPDATE ON platform_launch_permission_extensions
BEGIN SELECT RAISE(ABORT,'launch_permission_immutable'); END;
CREATE TRIGGER trg_platform_launch_permissions_delete_prd138
BEFORE DELETE ON platform_launch_permission_extensions
BEGIN SELECT RAISE(ABORT,'launch_permission_immutable'); END;
CREATE TRIGGER trg_platform_launch_roles_insert_prd138
BEFORE INSERT ON platform_launch_role_permissions
BEGIN SELECT RAISE(ABORT,'launch_permission_immutable'); END;
CREATE TRIGGER trg_platform_launch_roles_update_prd138
BEFORE UPDATE ON platform_launch_role_permissions
BEGIN SELECT RAISE(ABORT,'launch_permission_immutable'); END;
CREATE TRIGGER trg_platform_launch_roles_delete_prd138
BEFORE DELETE ON platform_launch_role_permissions
BEGIN SELECT RAISE(ABORT,'launch_permission_immutable'); END;

CREATE TABLE launch_gate_policies(
  launch_gate_id TEXT PRIMARY KEY CHECK(length(launch_gate_id)=36
    AND substr(launch_gate_id,1,4)='lgt_' AND substr(launch_gate_id,5) NOT GLOB '*[^0-9a-f]*'),
  gate_code TEXT NOT NULL UNIQUE CHECK(gate_code IN(
    'wb','api','bb','gr','perf','web','ui','ucd','comp','sec','flow','migration',
    'disaster_recovery','payment','edge','privacy','compliance','supply_chain',
    'tenant_isolation','long_stability')),
  policy_version INTEGER NOT NULL CHECK(policy_version=1),
  verifier_code TEXT NOT NULL UNIQUE CHECK(length(verifier_code) BETWEEN 5 AND 48),
  verifier_version INTEGER NOT NULL CHECK(verifier_version=1),
  threshold_rule TEXT NOT NULL CHECK(threshold_rule='exact_pass'),
  freshness_seconds INTEGER NOT NULL CHECK(freshness_seconds BETWEEN 300 AND 604800),
  blocking_severity TEXT NOT NULL CHECK(blocking_severity='p0'),
  policy_sha256 TEXT NOT NULL UNIQUE CHECK(length(policy_sha256)=64
    AND policy_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status='active'),
  seeded INTEGER NOT NULL CHECK(seeded=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
INSERT INTO launch_gate_policies VALUES
  ('lgt_5b27833bf16386d600e870a458b55005','wb',1,'wb-verifier',1,'exact_pass',86400,'p0','5b27833bf16386d600e870a458b55005e577fe1e65eb3c4e6d98503ee0f7addc','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_6c781a301aeac0f1ee729fce0ff917f4','api',1,'api-verifier',1,'exact_pass',86400,'p0','6c781a301aeac0f1ee729fce0ff917f4029d9846f79f3c461ed1f99750a75635','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_c675440bc0b9814b6f250d6ff5e4280d','bb',1,'bb-verifier',1,'exact_pass',86400,'p0','c675440bc0b9814b6f250d6ff5e4280d5f160bf5a2effa4c632d8c67753fedb3','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_50c87bd5952d34ee73efe40bba74514f','gr',1,'gr-verifier',1,'exact_pass',86400,'p0','50c87bd5952d34ee73efe40bba74514f17dc11063011880b6fb62d52c69cc289','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_06eaa1c3b6b3237d9b2c74635fe925d2','perf',1,'perf-verifier',1,'exact_pass',43200,'p0','06eaa1c3b6b3237d9b2c74635fe925d2148ce99e74c78340fecc1cd543429f8e','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_0f4e1eec8c83f98e3cf0ac6d3c01dabc','web',1,'web-verifier',1,'exact_pass',43200,'p0','0f4e1eec8c83f98e3cf0ac6d3c01dabc5de332046d99e4876c44b5b61b7afba6','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_a86dc62e844cf747a432412430991bb2','ui',1,'ui-verifier',1,'exact_pass',43200,'p0','a86dc62e844cf747a432412430991bb2bd77e394cd0543efb51a935d9d59fef7','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_a1c828435406a176d307dbd058488477','ucd',1,'ucd-verifier',1,'exact_pass',43200,'p0','a1c828435406a176d307dbd0584884775a507d17b6eee1d2c4ba81d516fddca3','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_84d6e6a7fdba9cb765bb3e7d3a31d472','comp',1,'comp-verifier',1,'exact_pass',43200,'p0','84d6e6a7fdba9cb765bb3e7d3a31d47202c7cc4fb166cf178f84a6ffc12318bb','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_faf3940c64394e4d604fd000ed76e57d','sec',1,'sec-verifier',1,'exact_pass',21600,'p0','faf3940c64394e4d604fd000ed76e57d2bf65de06b2e609da354c33c020e3ea1','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_3eea136f21a13f85d77993d7e5f16c63','flow',1,'flow-verifier',1,'exact_pass',43200,'p0','3eea136f21a13f85d77993d7e5f16c63a62bbd1c3f3d43f3bcf6309c88e622fb','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_3a0b769dd46888c3dd07615f7878f619','migration',1,'migration-verifier',1,'exact_pass',86400,'p0','3a0b769dd46888c3dd07615f7878f619adab5b046496c66862b8ac31bd292336','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_877d31960bd9bf8647ad2dddca69d20d','disaster_recovery',1,'disaster_recovery-verifier',1,'exact_pass',86400,'p0','877d31960bd9bf8647ad2dddca69d20d82a0c956ac411bc93c016cb7e936fd47','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_09e4c2609a6f8dcec4bd5b4b287f6473','payment',1,'payment-verifier',1,'exact_pass',86400,'p0','09e4c2609a6f8dcec4bd5b4b287f64737c7965c7dff1bc6f467e89db1fe89edc','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_5f43354a9a12a41a487ca88516d1b29a','edge',1,'edge-verifier',1,'exact_pass',86400,'p0','5f43354a9a12a41a487ca88516d1b29afcd95d6b62ab236929223874d2152bc6','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_4aa0706992893d3d3ae178a764c51f6d','privacy',1,'privacy-verifier',1,'exact_pass',86400,'p0','4aa0706992893d3d3ae178a764c51f6d9ac6bfec54ddf4465c883bd5dca5c053','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_6f279d32b806e2572798f00421c59463','compliance',1,'compliance-verifier',1,'exact_pass',86400,'p0','6f279d32b806e2572798f00421c59463d3eef151db042ff4e80ccba3b59096f5','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_e64e974a14afbb1785393307d3ab1231','supply_chain',1,'supply_chain-verifier',1,'exact_pass',86400,'p0','e64e974a14afbb1785393307d3ab12315a32e5c21fea189b3c0b80ed7bbabd87','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_8493d6ae9f30ddae398dcf85ba64ba16','tenant_isolation',1,'tenant_isolation-verifier',1,'exact_pass',21600,'p0','8493d6ae9f30ddae398dcf85ba64ba16008859d1b29cf3ab4834a30a1b7cc3f0','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('lgt_e6f8b9040dd497ee692cfa92729aa329','long_stability',1,'long_stability-verifier',1,'exact_pass',14400,'p0','e6f8b9040dd497ee692cfa92729aa329545ac3e3d6417c745e29735203816b30','active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_launch_gate_insert_prd138 BEFORE INSERT ON launch_gate_policies
BEGIN SELECT RAISE(ABORT,'launch_gate_immutable'); END;
CREATE TRIGGER trg_launch_gate_update_prd138 BEFORE UPDATE ON launch_gate_policies
BEGIN SELECT RAISE(ABORT,'launch_gate_immutable'); END;
CREATE TRIGGER trg_launch_gate_delete_prd138 BEFORE DELETE ON launch_gate_policies
BEGIN SELECT RAISE(ABORT,'launch_gate_immutable'); END;

CREATE TABLE launch_candidates(
  launch_candidate_id TEXT PRIMARY KEY CHECK(length(launch_candidate_id)=36
    AND substr(launch_candidate_id,1,4)='lvc_' AND substr(launch_candidate_id,5) NOT GLOB '*[^0-9a-f]*'),
  candidate_sha256 TEXT NOT NULL UNIQUE CHECK(length(candidate_sha256)=64),
  code_tree_sha256 TEXT NOT NULL CHECK(length(code_tree_sha256)=64),
  migration_set_sha256 TEXT NOT NULL CHECK(length(migration_set_sha256)=64),
  contract_set_sha256 TEXT NOT NULL CHECK(length(contract_set_sha256)=64),
  artifact_set_sha256 TEXT NOT NULL CHECK(length(artifact_set_sha256)=64),
  test_manifest_sha256 TEXT NOT NULL CHECK(length(test_manifest_sha256)=64),
  gate_policy_set_sha256 TEXT NOT NULL CHECK(length(gate_policy_set_sha256)=64),
  generation INTEGER NOT NULL UNIQUE CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN(
    'collecting','signing','ready','launching','paused','review_pending',
    'release_candidate','rollback_pending','rolled_back','failed','stale')),
  requested_by_operator_id TEXT NOT NULL,
  requested_permission_revision INTEGER NOT NULL CHECK(requested_permission_revision>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(requested_by_operator_id) REFERENCES platform_operators(operator_id)
);
CREATE TRIGGER trg_launch_candidate_update_prd138
BEFORE UPDATE ON launch_candidates FOR EACH ROW
WHEN NEW.launch_candidate_id!=OLD.launch_candidate_id
  OR NEW.candidate_sha256!=OLD.candidate_sha256
  OR NEW.code_tree_sha256!=OLD.code_tree_sha256
  OR NEW.migration_set_sha256!=OLD.migration_set_sha256
  OR NEW.contract_set_sha256!=OLD.contract_set_sha256
  OR NEW.artifact_set_sha256!=OLD.artifact_set_sha256
  OR NEW.test_manifest_sha256!=OLD.test_manifest_sha256
  OR NEW.gate_policy_set_sha256!=OLD.gate_policy_set_sha256
  OR NEW.generation!=OLD.generation
  OR NEW.requested_by_operator_id!=OLD.requested_by_operator_id
  OR NEW.requested_permission_revision!=OLD.requested_permission_revision
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR NOT(
    (OLD.status='collecting' AND NEW.status IN('collecting','signing','failed','stale'))
    OR (OLD.status='signing' AND NEW.status IN('signing','ready','failed','stale'))
    OR (OLD.status='ready' AND NEW.status IN('ready','launching','failed','stale'))
    OR (OLD.status='launching' AND NEW.status IN(
      'launching','paused','review_pending','rollback_pending','failed'))
    OR (OLD.status='paused' AND NEW.status IN('paused','launching','rollback_pending','failed'))
    OR (OLD.status='review_pending' AND NEW.status IN(
      'review_pending','release_candidate','rollback_pending','failed'))
    OR (OLD.status='release_candidate' AND NEW.status IN(
      'release_candidate','rollback_pending','stale'))
    OR (OLD.status='rollback_pending' AND NEW.status IN(
      'rollback_pending','rolled_back','failed'))
    OR (OLD.status='rolled_back' AND NEW.status='rolled_back')
    OR (OLD.status='failed' AND NEW.status='failed')
    OR (OLD.status='stale' AND NEW.status='stale'))
BEGIN SELECT RAISE(ABORT,'launch_candidate_transition_invalid'); END;
CREATE TRIGGER trg_launch_candidate_delete_prd138 BEFORE DELETE ON launch_candidates
BEGIN SELECT RAISE(ABORT,'launch_candidate_immutable'); END;

CREATE TABLE launch_candidate_artifacts(
  launch_candidate_artifact_id TEXT PRIMARY KEY CHECK(length(launch_candidate_artifact_id)=36
    AND substr(launch_candidate_artifact_id,1,4)='lca_'
    AND substr(launch_candidate_artifact_id,5) NOT GLOB '*[^0-9a-f]*'),
  launch_candidate_id TEXT NOT NULL,
  launch_gate_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  registered_at TEXT NOT NULL CHECK(unixepoch(registered_at) IS NOT NULL),
  FOREIGN KEY(launch_candidate_id) REFERENCES launch_candidates(launch_candidate_id),
  FOREIGN KEY(launch_gate_id) REFERENCES launch_gate_policies(launch_gate_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(launch_candidate_id,launch_gate_id),
  UNIQUE(launch_candidate_id,launch_gate_id,workspace_id,tenant_id,artifact_sha256)
);
CREATE TRIGGER trg_launch_artifact_update_prd138 BEFORE UPDATE ON launch_candidate_artifacts
BEGIN SELECT RAISE(ABORT,'launch_artifact_immutable'); END;
CREATE TRIGGER trg_launch_artifact_delete_prd138 BEFORE DELETE ON launch_candidate_artifacts
BEGIN SELECT RAISE(ABORT,'launch_artifact_immutable'); END;

CREATE TABLE launch_evidence(
  launch_evidence_id TEXT PRIMARY KEY CHECK(length(launch_evidence_id)=36
    AND substr(launch_evidence_id,1,4)='lve_' AND substr(launch_evidence_id,5) NOT GLOB '*[^0-9a-f]*'),
  launch_candidate_id TEXT NOT NULL,
  launch_gate_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  verifier_code TEXT NOT NULL,
  verifier_version INTEGER NOT NULL CHECK(verifier_version=1),
  envelope_sha256 TEXT NOT NULL UNIQUE CHECK(length(envelope_sha256)=64),
  signature_sha256 TEXT NOT NULL CHECK(length(signature_sha256)=64),
  outcome TEXT NOT NULL CHECK(outcome IN('passed','failed')),
  metric_value REAL,
  metric_unit TEXT CHECK(metric_unit IS NULL OR length(metric_unit) BETWEEN 1 AND 32),
  environment_sha256 TEXT NOT NULL CHECK(length(environment_sha256)=64),
  provenance_sha256 TEXT NOT NULL CHECK(length(provenance_sha256)=64),
  started_at TEXT NOT NULL CHECK(unixepoch(started_at) IS NOT NULL),
  finished_at TEXT NOT NULL CHECK(unixepoch(finished_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  submitted_by_operator_id TEXT NOT NULL,
  submitted_permission_revision INTEGER NOT NULL CHECK(submitted_permission_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(launch_candidate_id,launch_gate_id,workspace_id,tenant_id,artifact_sha256)
    REFERENCES launch_candidate_artifacts(
      launch_candidate_id,launch_gate_id,workspace_id,tenant_id,artifact_sha256),
  FOREIGN KEY(submitted_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(launch_candidate_id,launch_gate_id)
);
CREATE INDEX idx_launch_evidence_expiry_prd138 ON launch_evidence(expires_at);
CREATE TRIGGER trg_launch_evidence_update_prd138 BEFORE UPDATE ON launch_evidence
BEGIN SELECT RAISE(ABORT,'launch_evidence_immutable'); END;
CREATE TRIGGER trg_launch_evidence_delete_prd138 BEFORE DELETE ON launch_evidence
BEGIN SELECT RAISE(ABORT,'launch_evidence_immutable'); END;

CREATE TABLE launch_go_no_go_decisions(
  go_no_go_decision_id TEXT PRIMARY KEY CHECK(length(go_no_go_decision_id)=36
    AND substr(go_no_go_decision_id,1,4)='lgd_'
    AND substr(go_no_go_decision_id,5) NOT GLOB '*[^0-9a-f]*'),
  launch_candidate_id TEXT NOT NULL,
  actor_kind TEXT NOT NULL CHECK(actor_kind IN(
    'product','sre','security','test','finance','legal')),
  operator_id TEXT NOT NULL,
  operator_revision INTEGER NOT NULL CHECK(operator_revision>=1),
  permission_code TEXT NOT NULL CHECK(permission_code IN(
    'platform.launch.sign.product','platform.launch.sign.sre',
    'platform.launch.sign.security','platform.launch.sign.test',
    'platform.launch.sign.finance','platform.launch.sign.legal')),
  candidate_generation INTEGER NOT NULL CHECK(candidate_generation>=1),
  decision TEXT NOT NULL CHECK(decision IN('approved','rejected')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80),
  decision_sha256 TEXT NOT NULL UNIQUE CHECK(length(decision_sha256)=64),
  signature_sha256 TEXT NOT NULL UNIQUE CHECK(length(signature_sha256)=64),
  decided_at TEXT NOT NULL CHECK(unixepoch(decided_at) IS NOT NULL),
  FOREIGN KEY(launch_candidate_id) REFERENCES launch_candidates(launch_candidate_id),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(launch_candidate_id,actor_kind),
  UNIQUE(launch_candidate_id,operator_id)
);
CREATE TRIGGER trg_launch_decision_insert_prd138
BEFORE INSERT ON launch_go_no_go_decisions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM launch_candidates candidate
  WHERE candidate.launch_candidate_id=NEW.launch_candidate_id
    AND candidate.generation=NEW.candidate_generation)
BEGIN SELECT RAISE(ABORT,'launch_decision_generation_invalid'); END;
CREATE TRIGGER trg_launch_decision_update_prd138 BEFORE UPDATE ON launch_go_no_go_decisions
BEGIN SELECT RAISE(ABORT,'launch_decision_immutable'); END;
CREATE TRIGGER trg_launch_decision_delete_prd138 BEFORE DELETE ON launch_go_no_go_decisions
BEGIN SELECT RAISE(ABORT,'launch_decision_immutable'); END;

CREATE TABLE launch_waves(
  launch_wave_id TEXT PRIMARY KEY CHECK(length(launch_wave_id)=36
    AND substr(launch_wave_id,1,4)='lvw_' AND substr(launch_wave_id,5) NOT GLOB '*[^0-9a-f]*'),
  launch_candidate_id TEXT NOT NULL,
  wave_kind TEXT NOT NULL CHECK(wave_kind IN(
    'internal','personal_pilot','enterprise_pilot','managed_private_pilot')),
  wave_sequence INTEGER NOT NULL CHECK(wave_sequence BETWEEN 1 AND 4),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  cohort_manifest_sha256 TEXT NOT NULL CHECK(length(cohort_manifest_sha256)=64),
  feature_flag_generation INTEGER NOT NULL CHECK(feature_flag_generation>=1),
  migration_plan_sha256 TEXT NOT NULL CHECK(length(migration_plan_sha256)=64),
  rollback_plan_sha256 TEXT NOT NULL CHECK(length(rollback_plan_sha256)=64),
  observation_seconds INTEGER NOT NULL CHECK(observation_seconds BETWEEN 60 AND 604800),
  status TEXT NOT NULL CHECK(status IN('pending','running','paused','completed','rolled_back')),
  observation_deadline_at TEXT CHECK(
    observation_deadline_at IS NULL OR unixepoch(observation_deadline_at) IS NOT NULL),
  health_watermark_sha256 TEXT CHECK(
    health_watermark_sha256 IS NULL OR length(health_watermark_sha256)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(launch_candidate_id) REFERENCES launch_candidates(launch_candidate_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(launch_candidate_id,wave_kind),
  UNIQUE(launch_candidate_id,wave_sequence),
  UNIQUE(launch_candidate_id,wave_kind,workspace_id,tenant_id)
);
CREATE TRIGGER trg_launch_wave_update_prd138
BEFORE UPDATE ON launch_waves FOR EACH ROW
WHEN NEW.launch_wave_id!=OLD.launch_wave_id
  OR NEW.launch_candidate_id!=OLD.launch_candidate_id
  OR NEW.wave_kind!=OLD.wave_kind OR NEW.wave_sequence!=OLD.wave_sequence
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.cohort_manifest_sha256!=OLD.cohort_manifest_sha256
  OR NEW.feature_flag_generation!=OLD.feature_flag_generation
  OR NEW.migration_plan_sha256!=OLD.migration_plan_sha256
  OR NEW.rollback_plan_sha256!=OLD.rollback_plan_sha256
  OR NEW.observation_seconds!=OLD.observation_seconds
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='pending' AND NEW.status IN('pending','running','rolled_back'))
    OR (OLD.status='running' AND NEW.status IN('running','paused','completed','rolled_back'))
    OR (OLD.status='paused' AND NEW.status IN('paused','running','rolled_back'))
    OR (OLD.status='completed' AND NEW.status IN('completed','rolled_back'))
    OR (OLD.status='rolled_back' AND NEW.status='rolled_back'))
BEGIN SELECT RAISE(ABORT,'launch_wave_transition_invalid'); END;
CREATE TRIGGER trg_launch_wave_delete_prd138 BEFORE DELETE ON launch_waves
BEGIN SELECT RAISE(ABORT,'launch_wave_immutable'); END;

CREATE TABLE launch_rollbacks(
  launch_rollback_id TEXT PRIMARY KEY CHECK(length(launch_rollback_id)=36
    AND substr(launch_rollback_id,1,4)='lrb_' AND substr(launch_rollback_id,5) NOT GLOB '*[^0-9a-f]*'),
  launch_candidate_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  from_wave_kind TEXT NOT NULL CHECK(from_wave_kind IN(
    'internal','personal_pilot','enterprise_pilot','managed_private_pilot')),
  to_wave_kind TEXT NOT NULL CHECK(to_wave_kind IN(
    'pre_launch','internal','personal_pilot','enterprise_pilot')),
  trigger_code TEXT NOT NULL CHECK(trigger_code IN(
    'gate_failed','health_slo','payment_failure','security_finding','operator_request')),
  trigger_sha256 TEXT NOT NULL CHECK(length(trigger_sha256)=64),
  checkpoint_sha256 TEXT NOT NULL CHECK(length(checkpoint_sha256)=64),
  job_receipt_sha256 TEXT NOT NULL CHECK(length(job_receipt_sha256)=64),
  verification_sha256 TEXT CHECK(
    verification_sha256 IS NULL OR length(verification_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('pending','passed','failed')),
  requested_by_operator_id TEXT NOT NULL,
  requested_permission_revision INTEGER NOT NULL CHECK(requested_permission_revision>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  verified_at TEXT CHECK(verified_at IS NULL OR unixepoch(verified_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(launch_candidate_id,from_wave_kind,workspace_id,tenant_id)
    REFERENCES launch_waves(launch_candidate_id,wave_kind,workspace_id,tenant_id),
  FOREIGN KEY(requested_by_operator_id) REFERENCES platform_operators(operator_id)
);
CREATE TRIGGER trg_launch_rollback_update_prd138
BEFORE UPDATE ON launch_rollbacks FOR EACH ROW
WHEN NEW.launch_rollback_id!=OLD.launch_rollback_id
  OR NEW.launch_candidate_id!=OLD.launch_candidate_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.from_wave_kind!=OLD.from_wave_kind OR NEW.to_wave_kind!=OLD.to_wave_kind
  OR NEW.trigger_code!=OLD.trigger_code OR NEW.trigger_sha256!=OLD.trigger_sha256
  OR NEW.checkpoint_sha256!=OLD.checkpoint_sha256
  OR NEW.job_receipt_sha256!=OLD.job_receipt_sha256
  OR NEW.requested_by_operator_id!=OLD.requested_by_operator_id
  OR NEW.requested_permission_revision!=OLD.requested_permission_revision
  OR NEW.requested_at!=OLD.requested_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='pending' AND NEW.status IN('pending','passed','failed'))
    OR (OLD.status='passed' AND NEW.status='passed')
    OR (OLD.status='failed' AND NEW.status='failed'))
BEGIN SELECT RAISE(ABORT,'launch_rollback_transition_invalid'); END;
CREATE TRIGGER trg_launch_rollback_delete_prd138 BEFORE DELETE ON launch_rollbacks
BEGIN SELECT RAISE(ABORT,'launch_rollback_immutable'); END;

CREATE TABLE post_launch_reviews(
  post_launch_review_id TEXT PRIMARY KEY CHECK(length(post_launch_review_id)=36
    AND substr(post_launch_review_id,1,4)='lpr_' AND substr(post_launch_review_id,5) NOT GLOB '*[^0-9a-f]*'),
  launch_candidate_id TEXT NOT NULL UNIQUE,
  wave_kind TEXT NOT NULL CHECK(wave_kind='managed_private_pilot'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  summary_sha256 TEXT NOT NULL CHECK(length(summary_sha256)=64),
  action_items_sha256 TEXT NOT NULL CHECK(length(action_items_sha256)=64),
  conclusion TEXT NOT NULL CHECK(conclusion='release_candidate'),
  reviewed_by_operator_id TEXT NOT NULL,
  reviewed_permission_revision INTEGER NOT NULL CHECK(reviewed_permission_revision>=1),
  reviewed_at TEXT NOT NULL CHECK(unixepoch(reviewed_at) IS NOT NULL),
  FOREIGN KEY(launch_candidate_id,wave_kind,workspace_id,tenant_id)
    REFERENCES launch_waves(launch_candidate_id,wave_kind,workspace_id,tenant_id),
  FOREIGN KEY(reviewed_by_operator_id) REFERENCES platform_operators(operator_id)
);
CREATE TRIGGER trg_launch_review_update_prd138 BEFORE UPDATE ON post_launch_reviews
BEGIN SELECT RAISE(ABORT,'launch_review_immutable'); END;
CREATE TRIGGER trg_launch_review_delete_prd138 BEFORE DELETE ON post_launch_reviews
BEGIN SELECT RAISE(ABORT,'launch_review_immutable'); END;

CREATE TABLE launch_events(
  launch_event_id TEXT PRIMARY KEY CHECK(length(launch_event_id)=36
    AND substr(launch_event_id,1,4)='lev_' AND substr(launch_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  launch_candidate_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'launch.gate_failed','launch.wave_started','launch.wave_completed','launch.rolled_back')),
  object_type TEXT NOT NULL CHECK(object_type IN('evidence','wave','rollback')),
  object_id TEXT NOT NULL CHECK(length(object_id)=36),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(launch_candidate_id) REFERENCES launch_candidates(launch_candidate_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(launch_candidate_id,event_type,object_id)
);
CREATE TRIGGER trg_launch_event_owner_insert_prd138
BEFORE INSERT ON launch_events FOR EACH ROW
WHEN (NEW.object_type='evidence' AND NOT EXISTS(
    SELECT 1 FROM launch_evidence evidence WHERE evidence.launch_evidence_id=NEW.object_id
      AND evidence.launch_candidate_id=NEW.launch_candidate_id
      AND evidence.workspace_id=NEW.workspace_id AND evidence.tenant_id=NEW.tenant_id))
  OR (NEW.object_type='wave' AND NOT EXISTS(
    SELECT 1 FROM launch_waves wave WHERE wave.launch_wave_id=NEW.object_id
      AND wave.launch_candidate_id=NEW.launch_candidate_id
      AND wave.workspace_id=NEW.workspace_id AND wave.tenant_id=NEW.tenant_id))
  OR (NEW.object_type='rollback' AND NOT EXISTS(
    SELECT 1 FROM launch_rollbacks rollback WHERE rollback.launch_rollback_id=NEW.object_id
      AND rollback.launch_candidate_id=NEW.launch_candidate_id
      AND rollback.workspace_id=NEW.workspace_id AND rollback.tenant_id=NEW.tenant_id))
BEGIN SELECT RAISE(ABORT,'launch_event_owner_invalid'); END;
CREATE TRIGGER trg_launch_event_update_prd138 BEFORE UPDATE ON launch_events
BEGIN SELECT RAISE(ABORT,'launch_event_immutable'); END;
CREATE TRIGGER trg_launch_event_delete_prd138 BEFORE DELETE ON launch_events
BEGIN SELECT RAISE(ABORT,'launch_event_immutable'); END;

CREATE TABLE launch_event_outbox(
  launch_event_outbox_id TEXT PRIMARY KEY CHECK(length(launch_event_outbox_id)=36
    AND substr(launch_event_outbox_id,1,4)='leo_'
    AND substr(launch_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  launch_event_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','delivered')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(launch_event_id) REFERENCES launch_events(launch_event_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_launch_outbox_due_prd138 ON launch_event_outbox(status,next_attempt_at);
CREATE TRIGGER trg_launch_outbox_owner_insert_prd138
BEFORE INSERT ON launch_event_outbox FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM launch_events event
  WHERE event.launch_event_id=NEW.launch_event_id
    AND event.workspace_id=NEW.workspace_id AND event.tenant_id=NEW.tenant_id)
BEGIN SELECT RAISE(ABORT,'launch_outbox_owner_invalid'); END;
CREATE TRIGGER trg_launch_outbox_update_prd138
BEFORE UPDATE ON launch_event_outbox FOR EACH ROW
WHEN NEW.launch_event_id!=OLD.launch_event_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.attempt_count<OLD.attempt_count OR NEW.created_at!=OLD.created_at
  OR NOT((OLD.status='pending' AND NEW.status IN('pending','delivered'))
    OR (OLD.status='delivered' AND NEW.status='delivered'))
BEGIN SELECT RAISE(ABORT,'launch_outbox_transition_invalid'); END;
CREATE TRIGGER trg_launch_outbox_delete_prd138 BEFORE DELETE ON launch_event_outbox
BEGIN SELECT RAISE(ABORT,'launch_outbox_immutable'); END;

CREATE TABLE launch_action_commands(
  launch_action_command_id TEXT PRIMARY KEY CHECK(length(launch_action_command_id)=36
    AND substr(launch_action_command_id,1,4)='lac_'
    AND substr(launch_action_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  operator_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'candidate.create','evidence.submit','decision.sign','wave.start','wave.pause',
    'wave.resume','wave.complete','candidate.review','rollback.request','rollback.verify')),
  idempotency_key_sha256 TEXT NOT NULL CHECK(length(idempotency_key_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(operator_id,action_code,idempotency_key_sha256)
);
CREATE TRIGGER trg_launch_action_update_prd138 BEFORE UPDATE ON launch_action_commands
BEGIN SELECT RAISE(ABORT,'launch_action_immutable'); END;
CREATE TRIGGER trg_launch_action_delete_prd138 BEFORE DELETE ON launch_action_commands
BEGIN SELECT RAISE(ABORT,'launch_action_immutable'); END;

CREATE TABLE launch_scheduler_state(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  freshness_watermark_at TEXT NOT NULL CHECK(unixepoch(freshness_watermark_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_token_sha256 TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK((lease_owner_id IS NULL AND lease_token_sha256 IS NULL AND lease_expires_at IS NULL)
    OR(lease_owner_id IS NOT NULL AND length(lease_token_sha256)=64
      AND lease_expires_at IS NOT NULL))
);
INSERT INTO launch_scheduler_state VALUES(
  1,'1970-01-01T00:00:00.000Z',NULL,NULL,NULL,0,1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_launch_scheduler_update_prd138
BEFORE UPDATE ON launch_scheduler_state FOR EACH ROW
WHEN NEW.singleton_id!=OLD.singleton_id OR NEW.fencing_generation<OLD.fencing_generation
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'launch_scheduler_fenced'); END;
CREATE TRIGGER trg_launch_scheduler_delete_prd138 BEFORE DELETE ON launch_scheduler_state
BEGIN SELECT RAISE(ABORT,'launch_scheduler_required'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd138(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-138'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd138_manifest_sources(
  source_table TEXT PRIMARY KEY,disposition TEXT NOT NULL,retention_class TEXT NOT NULL);
INSERT INTO prd138_manifest_sources VALUES
  ('launch_candidate_artifacts','archive_then_delete','operational'),
  ('launch_evidence','archive_then_delete','operational'),
  ('launch_waves','archive_then_delete','operational'),
  ('launch_rollbacks','archive_then_delete','operational'),
  ('post_launch_reviews','archive_then_delete','operational'),
  ('launch_events','archive_then_delete','audit'),
  ('launch_event_outbox','archive_then_delete','audit'),
  ('launch_action_commands','archive_then_delete','audit');
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
JOIN prd138_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd138
SELECT source_table,'tenant_id','PRD-138',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd138_manifest_sources ORDER BY source_table;
DROP TABLE prd138_manifest_sources;
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
CREATE TRIGGER trg_tenant_deletion_manifest_prd138_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd138
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd138_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd138
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd138_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd138
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd138_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd138_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_launch_permission_extensions)=11
      AND (SELECT COUNT(*) FROM platform_launch_role_permissions)=17
      AND (SELECT COUNT(*) FROM launch_gate_policies)=20
      AND (SELECT COUNT(*) FROM launch_scheduler_state)=1
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd138)=8
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd138_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
